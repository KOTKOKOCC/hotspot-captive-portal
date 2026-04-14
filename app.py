from fastapi import FastAPI, HTTPException, Query, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
from html import escape
from collections import OrderedDict
import re
import threading
import ipaddress


from labels import (
    COLUMN_LABELS,
    EVENT_LABELS,
    RESULT_LABELS,
    AUTH_METHOD_LABELS,
    STATUS_LABELS,
    TERMINATE_CAUSE_LABELS,
)

from db import (
    db,
    fetch_all,
    fetch_one,
    init_db,
    ensure_guests_last_auth_at,
    ensure_guest_sessions_device_name,
)

from exports import (
    rows_to_xlsx_bytes,
    build_single_xlsx,
    build_export_zip,
)

from mikrotik_api import (
    sync_session_device_names,
    start_device_name_sync_worker,
    fetch_dhcp_leases,
    mt_api,
)

from config import (
    APP_NAME,
    APP_SECRET,
    ADMIN_USERNAME,
    ADMIN_PASSWORD,
    ADMIN_COOKIE,
    DEVICE_LIMIT,
    PENDING_MINUTES,
)

from admin_auth import make_admin_token, check_admin_token, admin_guard

from services import (
    DISPLAY_TZ,
    normalize_accounting_event_time,
    resolve_network_info,
    audit,
    save_radius_accounting,
    cleanup_db,
    cleanup_stale_sessions,
    run_cleanup,
    get_guest_last_activity,
    accept_reply,
)

from auth import (
    now,
    now_iso,
    normalize_phone,
    normalize_mac,
    get_active_guest,
    get_or_create_guest,
    touch_guest_auth,
    get_live_pending,
    get_open_session,
    active_sessions_count,
    start_session,
    update_session,
)

from ui import (
    format_dt,
    html_table,
    admin_page,
)




app = FastAPI()


class CallIn(BaseModel):
    phone: str


class RadiusCheckIn(BaseModel):
    username: str
    mac: str
    ip: str | None = None
    nas_id: str | None = None
    hotel: str | None = None
    ssid: str | None = None


class RadiusAccountingIn(BaseModel):
    acct_status_type: str
    acct_session_id: str | None = None
    username: str | None = None
    mac: str | None = None
    ip: str | None = None
    nas_ip: str | None = None
    nas_id: str | None = None
    nas_port_id: str | None = None
    called_station_id: str | None = None
    terminate_cause: str | None = None
    session_time: int | None = None
    event_time: str | None = None


@app.get("/")
def root():
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page():
    return HTMLResponse("""
    <!doctype html>
    <html lang="ru">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Вход в админку</title>
      <style>
        body { font-family: Arial, sans-serif; background:#f5f7fb; margin:0; }
        .wrap { max-width:420px; margin:80px auto; padding:20px; }
        .card { background:#fff; border-radius:16px; padding:24px; box-shadow:0 10px 30px rgba(0,0,0,.08); }
        h1 { margin:0 0 18px; font-size:26px; text-align:center; }
        label { display:block; margin:12px 0 6px; font-weight:600; }
        input { width:100%; height:44px; box-sizing:border-box; padding:0 12px; border:1px solid #cbd5e1; border-radius:10px; }
        button { width:100%; height:46px; margin-top:16px; border:0; border-radius:10px; background:#1f4fd6; color:#fff; font-weight:700; font-size:16px; }
      </style>
    </head>
    <body>
      <div class="wrap">
        <div class="card">
          <h1>Вход в админку</h1>
          <form method="post" action="/admin/login">
            <label>Логин</label>
            <input type="text" name="username" required>
            <label>Пароль</label>
            <input type="password" name="password" required>
            <button type="submit">Войти</button>
          </form>
        </div>
      </div>
    </body>
    </html>
    """)


@app.post("/admin/login")
def admin_login(username: str = Form(...), password: str = Form(...)):
    if username != ADMIN_USERNAME or password != ADMIN_PASSWORD:
        return HTMLResponse("Неверный логин или пароль", status_code=401)

    resp = RedirectResponse(url="/admin", status_code=303)
    resp.set_cookie(
        key=ADMIN_COOKIE,
        value=make_admin_token(username),
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=60 * 60 * 8
    )
    return resp


@app.get("/admin/logout")
def admin_logout():
    resp = RedirectResponse(url="/admin/login", status_code=303)
    resp.delete_cookie(ADMIN_COOKIE)
    return resp


@app.get("/admin/export/full")
def admin_export_full(request: Request):
    guard = admin_guard(request)
    if guard:
        return guard

    data = build_export_zip()
    return StreamingResponse(
        data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="hotspot_export_full.zip"'}
    )


@app.get("/admin/export/period")
def admin_export_period(request: Request, date_from: str | None = None, date_to: str | None = None):
    guard = admin_guard(request)
    if guard:
        return guard

    data = build_export_zip(date_from=date_from, date_to=date_to)
    filename = f"hotspot_export_{date_from or 'start'}_{date_to or 'end'}.zip"
    return StreamingResponse(
        data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@app.on_event("startup")
def startup():
    init_db()
    ensure_guests_last_auth_at()
    ensure_guest_sessions_device_name()
    run_cleanup()

    try:
        sync_session_device_names()
    except Exception as e:
        print(f"[mikrotik-sync] startup sync skipped: {e}")

    try:
        start_device_name_sync_worker()
    except Exception as e:
        print(f"[mikrotik-sync] worker start skipped: {e}")


@app.post("/pbx-call")
def pbx_call(payload: CallIn):
    try:
        phone = normalize_phone(payload.phone)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid phone")

    conn = db()

    row = conn.execute("""
        SELECT * FROM pending_auth
        WHERE phone = ? AND status = 'pending'
        ORDER BY id DESC
        LIMIT 1
    """, (phone,)).fetchone()

    if not row:
        conn.execute("""
            INSERT INTO call_events (phone, callerid_raw, source_ip, created_at, result)
            VALUES (?, ?, ?, ?, ?)
        """, (phone, phone, None, now_iso(), "no_pending"))
        conn.commit()
        conn.close()
        audit("call_no_pending", phone=phone, details="PBX call without pending auth")
        raise HTTPException(status_code=404, detail="pending not found")

    if now() > datetime.fromisoformat(row["expires_at"]):
        conn.execute("UPDATE pending_auth SET status='expired' WHERE id=?", (row["id"],))
        conn.execute("""
            INSERT INTO call_events (phone, callerid_raw, source_ip, created_at, result)
            VALUES (?, ?, ?, ?, ?)
        """, (phone, phone, None, now_iso(), "expired_pending"))
        conn.commit()
        conn.close()
        audit("call_expired_pending", phone=phone, mac=row["mac"], ip=row["ip"], nas_id=row["nas_id"], hotel=row["hotel"], ssid=row["ssid"], vlan_id=row["vlan_id"], details="PBX call matched expired pending")
        raise HTTPException(status_code=410, detail="pending expired")

    guest = get_or_create_guest(phone, row["hotel"])

    conn.execute("UPDATE pending_auth SET status='verified' WHERE id = ?", (row["id"],))
    conn.execute("""
        UPDATE pending_auth
        SET status = 'verified'
        WHERE phone = ? AND id != ? AND status = 'pending'
    """, (phone, row["id"]))
    conn.execute("""
        INSERT INTO call_events (phone, callerid_raw, source_ip, created_at, result)
        VALUES (?, ?, ?, ?, ?)
    """, (phone, phone, None, now_iso(), "matched_pending"))
    conn.commit()
    conn.close()

    audit("call_verified", phone=phone, mac=row["mac"], ip=row["ip"], nas_id=row["nas_id"], hotel=row["hotel"], ssid=row["ssid"], vlan_id=row["vlan_id"], details=f"Phone verified by PBX call, guest_id={guest['id']}")

    return {
        "status": "verified",
        "phone": phone,
        "guest_id": guest["id"]
    }


@app.post("/radius-check")
def radius_check(payload: RadiusCheckIn):
    mac = normalize_mac(payload.mac)
    netinfo = resolve_network_info(payload.ip)
    hotel = netinfo["hotel_name"]
    ssid = netinfo["ssid_name"]
    vlan_id = netinfo["vlan_id"]

    try:
        phone = normalize_phone(payload.username)
    except ValueError:
        audit(
            "radius_reject_invalid_phone",
            phone=payload.username,
            mac=mac,
            ip=payload.ip,
            nas_id=payload.nas_id,
            hotel=hotel,
            ssid=ssid,
            vlan_id=vlan_id,
            details=f"Пользователь ввёл: {payload.username}"
        )
        raise HTTPException(status_code=403, detail="invalid_phone")

    guest = get_active_guest(phone)
    if guest:
        touch_guest_auth(phone)
        session = get_open_session(phone, mac)

        if session:
            update_session(session["id"], payload.ip, payload.nas_id, hotel, ssid, vlan_id)
            audit(
                "radius_accept_existing_session",
                phone=phone,
                mac=mac,
                ip=payload.ip,
                nas_id=payload.nas_id,
                hotel=hotel,
                ssid=ssid,
                vlan_id=vlan_id,
                details=f"Session id={session['id']}"
            )
            return accept_reply()

        if active_sessions_count(phone) >= DEVICE_LIMIT:
            audit(
                "radius_reject_device_limit",
                phone=phone,
                mac=mac,
                ip=payload.ip,
                nas_id=payload.nas_id,
                hotel=hotel,
                ssid=ssid,
                vlan_id=vlan_id,
                details=f"Device limit reached: {DEVICE_LIMIT}"
            )
            raise HTTPException(status_code=403, detail="device_limit")

        start_session(guest["id"], phone, mac, payload.ip, payload.nas_id, hotel, ssid, vlan_id)
        audit(
            "radius_accept_new_session",
            phone=phone,
            mac=mac,
            ip=payload.ip,
            nas_id=payload.nas_id,
            hotel=hotel,
            ssid=ssid,
            vlan_id=vlan_id,
            details=f"Guest id={guest['id']}"
        )
        return accept_reply()

    pending = get_live_pending(phone, mac)
    if pending:
        try:
            exp = datetime.fromisoformat(pending["expires_at"])
        except Exception:
            exp = None

        if exp:
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)

            if now() > exp:
                conn = db()
                conn.execute(
                    "UPDATE pending_auth SET status='expired' WHERE id=?",
                    (pending["id"],)
                )
                conn.commit()
                conn.close()

                audit(
                    "radius_reject_pending_expired",
                    phone=phone,
                    mac=mac,
                    ip=payload.ip,
                    nas_id=payload.nas_id,
                    hotel=hotel,
                    ssid=ssid,
                    vlan_id=vlan_id,
                    details="Pending expired"
                )
                raise HTTPException(status_code=403, detail="pending_expired")

        audit(
            "radius_reject_pending_waiting_call",
            phone=phone,
            mac=mac,
            ip=payload.ip,
            nas_id=payload.nas_id,
            hotel=hotel,
            ssid=ssid,
            vlan_id=vlan_id,
            details="Pending exists, waiting for call"
        )
        raise HTTPException(status_code=403, detail="pending_waiting_call")

    expires = now() + timedelta(minutes=PENDING_MINUTES)

    conn = db()
    conn.execute("""
        INSERT INTO pending_auth
        (phone, mac, ip, nas_id, hotel, ssid, vlan_id, created_at, expires_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        phone, mac, payload.ip, payload.nas_id, hotel, ssid, vlan_id,
        now_iso(), expires.isoformat(), "pending"
    ))
    conn.commit()
    conn.close()

    audit(
        "pending_created",
        phone=phone,
        mac=mac,
        ip=payload.ip,
        nas_id=payload.nas_id,
        hotel=hotel,
        ssid=ssid,
        vlan_id=vlan_id,
        details="Pending created on first radius-check"
    )
    raise HTTPException(status_code=403, detail="pending_created")


@app.get("/auth-status")
def auth_status(phone: str = Query(...)):
    try:
        phone = normalize_phone(phone)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid phone")

    guest = get_active_guest(phone)
    if guest:
        return {"status": "verified"}

    conn = db()
    pending = conn.execute("""
        SELECT * FROM pending_auth
        WHERE phone = ? AND status = 'pending'
        ORDER BY id DESC
        LIMIT 1
    """, (phone,)).fetchone()

    if pending:
        if now() > datetime.fromisoformat(pending["expires_at"]):
            conn.execute("UPDATE pending_auth SET status='expired' WHERE id=?", (pending["id"],))
            conn.commit()
            conn.close()
            return {"status": "expired"}
        conn.close()
        return {"status": "pending"}

    conn.close()
    return {"status": "not_found"}


@app.post("/radius-accounting")
def radius_accounting(payload: RadiusAccountingIn):
    save_radius_accounting(payload)
    return {"status": "ok"}


@app.get("/admin", response_class=HTMLResponse)
def admin_index(request: Request):
    guard = admin_guard(request)
    if guard:
        return guard

    guests_cnt = fetch_all("SELECT COUNT(*) AS cnt FROM guests")[0]["cnt"]
    sessions_cnt = fetch_all("SELECT COUNT(*) AS cnt FROM guest_sessions WHERE status='active' AND ended_at IS NULL")[0]["cnt"]
    pending_cnt = fetch_all("SELECT COUNT(*) AS cnt FROM pending_auth WHERE status='pending'")[0]["cnt"]
    calls_cnt = fetch_all("SELECT COUNT(*) AS cnt FROM call_events WHERE date(created_at)=date('now')")[0]["cnt"]

    body = f"""
    <div class="stats">
      <div class="stat">
        <div class="stat-label">Гостей в базе</div>
        <div class="stat-value" id="stat-guests">{guests_cnt}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Активных сессий</div>
        <div class="stat-value" id="stat-sessions">{sessions_cnt}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Ожидают звонка</div>
        <div class="stat-value" id="stat-pending">{pending_cnt}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Звонков сегодня</div>
        <div class="stat-value" id="stat-calls">{calls_cnt}</div>
      </div>
    </div>

    <div class="card" style="margin-top:18px;">
      <h2 style="margin:0 0 12px; font-size:24px;">Активность подключений</h2>

      <div style="display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:14px; flex-wrap:wrap;">
        <div style="font-size:14px; color:#6b7280;">Период отображения</div>
        <select id="chartPeriod" style="height:42px; padding:0 12px; border:1px solid #cbd5e1; border-radius:10px; background:#fff;">
          <option value="1h">Час</option>
          <option value="1d" selected>День</option>
          <option value="1mo">Месяц</option>
          <option value="1y">Год</option>
        </select>
      </div>

      <div style="width:100%; height:360px;">
        <canvas id="activityChart"></canvas>
      </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script>
      let activityChart = null;

      async function loadDashboardData() {{
        try {{
          const period = document.getElementById('chartPeriod').value;
          const resp = await fetch('/admin/dashboard-data?period=' + encodeURIComponent(period), {{ cache: 'no-store' }});
          if (!resp.ok) return;

          const data = await resp.json();

          document.getElementById('stat-guests').textContent = data.stats.guests;
          document.getElementById('stat-sessions').textContent = data.stats.active_sessions;
          document.getElementById('stat-pending').textContent = data.stats.pending;
          document.getElementById('stat-calls').textContent = data.stats.calls_today;

          const labels = data.chart.labels;
          const values = data.chart.values;

          if (!activityChart) {{
            const ctx = document.getElementById('activityChart').getContext('2d');
            activityChart = new Chart(ctx, {{
              type: 'line',
              data: {{
                labels: labels,
                datasets: [{{
                  label: 'Подключения',
                  data: values,
                  tension: 0.35,
                  fill: true,
                  borderWidth: 3,
                  pointRadius: 0
                }}]
              }},
              options: {{
                responsive: true,
                maintainAspectRatio: false,
                animation: {{
                  duration: 500
                }},
                plugins: {{
                  legend: {{
                    display: false
                  }}
                }},
                scales: {{
                  x: {{
                    grid: {{
                      display: false
                    }}
                  }},
                  y: {{
                    beginAtZero: true,
                    ticks: {{
                      precision: 0
                    }}
                  }}
                }}
              }}
            }});
          }} else {{
            activityChart.data.labels = labels;
            activityChart.data.datasets[0].data = values;
            activityChart.update();
          }}
        }} catch (e) {{
          console.error('dashboard update failed', e);
        }}
      }}

      document.getElementById('chartPeriod').addEventListener('change', loadDashboardData);

      loadDashboardData();
      setInterval(loadDashboardData, 30000);
    </script>
    """
    return admin_page("Панель управления", body, active_tab="home")

@app.get("/admin/guests", response_class=HTMLResponse)
def admin_guests():
    rows = fetch_all("SELECT * FROM guests ORDER BY created_at DESC LIMIT 300")
    cols = ["id", "phone", "first_verified_at", "first_hotel", "auth_method", "status", "created_at", "updated_at"]
    body = html_table(rows, cols)
    return admin_page("Гости", body, active_tab="guests")

@app.get("/admin/sessions", response_class=HTMLResponse)
def admin_sessions(
    request: Request,
    q: str = "",
    status: str = "all",
    hotel: str = "",
    terminate_cause: str = "",
    date_from: str = "",
    date_to: str = "",
):
    guard = admin_guard(request)
    if guard:
        return guard

    where = []
    params = []

    raw_q = q.strip()
    if raw_q:
        digits_q = re.sub(r"\D", "", raw_q)
        normalized_phone = None
        if len(digits_q) == 10:
            normalized_phone = "7" + digits_q
        elif len(digits_q) == 11 and digits_q.startswith("8"):
            normalized_phone = "7" + digits_q[1:]
        elif len(digits_q) == 11 and digits_q.startswith("7"):
            normalized_phone = digits_q

        patterns = [f"%{raw_q}%"]
        if digits_q:
            patterns.append(f"%{digits_q}%")
        if normalized_phone:
            patterns.append(f"%{normalized_phone}%")
        patterns = list(dict.fromkeys(patterns))

        q_parts = []
        for pattern in patterns:
            q_parts.append("(phone LIKE ? OR mac LIKE ? OR ip LIKE ? OR acct_session_id LIKE ? OR nas_id LIKE ?)")
            params.extend([pattern, pattern, pattern, pattern, pattern])

        where.append("(" + " OR ".join(q_parts) + ")")

    if status == "active":
        where.append("status = 'active'")
    elif status == "closed":
        where.append("status = 'closed'")

    if hotel.strip():
        where.append("hotel = ?")
        params.append(hotel.strip())

    if terminate_cause.strip():
        where.append("terminate_cause = ?")
        params.append(terminate_cause.strip())

    if date_from:
        where.append("date(started_at) >= date(?)")
        params.append(date_from)

    if date_to:
        where.append("date(started_at) <= date(?)")
        params.append(date_to)

    where_sql = ""
    if where:
        where_sql = "WHERE " + " AND ".join(where)

    rows = fetch_all(
        f"""
        SELECT * FROM guest_sessions
        {where_sql}
        ORDER BY CASE WHEN status='active' THEN 0 ELSE 1 END, started_at DESC
        LIMIT 500
        """,
        tuple(params)
    )

    hotel_rows = fetch_all("""
        SELECT DISTINCT hotel
        FROM guest_sessions
        WHERE hotel IS NOT NULL AND hotel != ''
        ORDER BY hotel
    """)
    hotels = [r["hotel"] for r in hotel_rows]

    cause_rows = fetch_all("""
        SELECT DISTINCT terminate_cause
        FROM guest_sessions
        WHERE terminate_cause IS NOT NULL AND terminate_cause != ''
        ORDER BY terminate_cause
    """)
    causes = [r["terminate_cause"] for r in cause_rows]

    hotel_options = ['<option value="">Все объекты</option>']
    for h in hotels:
        selected = " selected" if h == hotel else ""
        hotel_options.append(f'<option value="{escape(h)}"{selected}>{escape(h)}</option>')

    cause_options = ['<option value="">Все причины</option>']
    for c in causes:
        selected = " selected" if c == terminate_cause else ""
        cause_options.append(f'<option value="{escape(c)}"{selected}>{escape(TERMINATE_CAUSE_LABELS.get(c, c))}</option>')

    status_options = []
    for value, label in [("all", "Все"), ("active", "Только активные"), ("closed", "Только закрытые")]:
        selected = " selected" if value == status else ""
        status_options.append(f'<option value="{value}"{selected}>{label}</option>')

    body = f"""
    <div class="toolbar">
      <form method="get" action="/admin/sessions" style="display:flex; gap:10px; flex-wrap:wrap; align-items:end;">
        <div>
          <label style="display:block; margin-bottom:6px; font-weight:600;">Поиск</label>
          <input type="text" name="q" value="{escape(q)}" placeholder="Номер, MAC, IP, session ID">
        </div>

        <div>
          <label style="display:block; margin-bottom:6px; font-weight:600;">Статус</label>
          <select name="status">
            {''.join(status_options)}
          </select>
        </div>

        <div>
          <label style="display:block; margin-bottom:6px; font-weight:600;">Объект</label>
          <select name="hotel">
            {''.join(hotel_options)}
          </select>
        </div>

        <div>
          <label style="display:block; margin-bottom:6px; font-weight:600;">Причина завершения</label>
          <select name="terminate_cause">
            {''.join(cause_options)}
          </select>
        </div>

        <div>
          <label style="display:block; margin-bottom:6px; font-weight:600;">Дата с</label>
          <input type="date" name="date_from" value="{escape(date_from)}">
        </div>

        <div>
          <label style="display:block; margin-bottom:6px; font-weight:600;">Дата по</label>
          <input type="date" name="date_to" value="{escape(date_to)}">
        </div>

        <div>
          <button class="btn primary" type="submit">Применить</button>
        </div>

        <div>
          <a class="btn" href="/admin/sessions">Сбросить</a>
        </div>
      </form>
    </div>

    <div class="muted" style="margin-bottom:14px;">
      Показано сессий: {len(rows)}
    </div>
    """

    body += html_table(
        rows,
        [
            "guest_id",
            "phone",
            "mac",
            "ip",
            "device_name",
            "started_at",
            "last_seen_at",
            "ended_at",
            "status",
            "terminate_cause",
            "acct_session_time",
            "hotel",
            "ssid",
            "vlan_id",
            "nas_id",
            "acct_session_id",
        ]
    )

    return admin_page("Сессии", body, active_tab="sessions")


@app.get("/admin/pending", response_class=HTMLResponse)
def admin_pending():
    rows = fetch_all("SELECT * FROM pending_auth WHERE status = 'pending' ORDER BY created_at DESC LIMIT 300")
    cols = ["id", "phone", "mac", "ip", "nas_id", "hotel", "ssid", "vlan_id", "created_at", "expires_at", "status"]
    body = html_table(rows, cols)
    return admin_page("Ожидают подтверждения", body, active_tab="pending")


@app.get("/admin/calls", response_class=HTMLResponse)
def admin_calls():
    rows = fetch_all("SELECT * FROM call_events ORDER BY created_at DESC LIMIT 300")
    cols = ["id", "phone", "callerid_raw", "source_ip", "created_at", "result"]
    body = html_table(rows, cols)
    return admin_page("Звонки", body, active_tab="calls")


@app.get("/admin/audit", response_class=HTMLResponse)
def admin_audit():
    rows = fetch_all("SELECT * FROM audit_log ORDER BY event_time DESC LIMIT 500")
    cols = ["id", "phone", "mac", "ip", "nas_id", "hotel", "ssid", "vlan_id", "event_type", "event_time", "details"]
    body = html_table(rows, cols)
    return admin_page("Аудит", body, active_tab="audit")


@app.get("/admin/networks", response_class=HTMLResponse)
def admin_networks(request: Request, error: str = "", ok: str = "", edit_id: str = ""):
    guard = admin_guard(request)
    if guard:
        return guard

    def r(row, key, default=""):
        value = row[key]
        return default if value is None else value

    rows = fetch_all("""
        SELECT *
        FROM network_map
        ORDER BY CAST(COALESCE(vlan_id, '0') AS INTEGER), hotel_name, ssid_name
    """)

    edit_row = None
    if str(edit_id).strip().isdigit():
        edit_row = fetch_one("SELECT * FROM network_map WHERE id = ?", (int(edit_id),))

    msg_html = ""
    if error:
        msg_html += f'<div style="margin-bottom:12px; padding:12px 14px; border-radius:10px; background:#fee2e2; color:#991b1b;">{escape(error)}</div>'
    if ok:
        msg_html += f'<div style="margin-bottom:12px; padding:12px 14px; border-radius:10px; background:#dcfce7; color:#166534;">{escape(ok)}</div>'

    add_form = """
    <div class="toolbar" style="margin-bottom:16px;">
      <form method="post" action="/admin/networks/add" style="display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:12px; width:100%;">
        <div>
          <label style="display:block; margin-bottom:6px; font-weight:600;">Объект</label>
          <input type="text" name="hotel_name" placeholder="Например, Корпус А" required>
        </div>

        <div>
          <label style="display:block; margin-bottom:6px; font-weight:600;">Wi-Fi сеть</label>
          <input type="text" name="ssid_name" placeholder="Например, Guest Wi-Fi" required>
        </div>

        <div>
          <label style="display:block; margin-bottom:6px; font-weight:600;">VLAN</label>
          <input type="text" name="vlan_id" placeholder="Например, 120">
        </div>

        <div>
          <label style="display:block; margin-bottom:6px; font-weight:600;">Подсеть</label>
          <input type="text" name="subnet_cidr" placeholder="Например, 10.10.120.0/24" required>
        </div>

        <div>
          <label style="display:block; margin-bottom:6px; font-weight:600;">Интерфейс MikroTik</label>
          <input type="text" name="mikrotik_interface" placeholder="Например, vlan120-guest">
        </div>

        <div>
          <label style="display:block; margin-bottom:6px; font-weight:600;">Hotspot server</label>
          <input type="text" name="hotspot_server" placeholder="Например, hs-guest-120">
        </div>

        <div>
          <label style="display:block; margin-bottom:6px; font-weight:600;">Активна</label>
          <select name="is_active" style="height:42px;">
            <option value="1" selected>Да</option>
            <option value="0">Нет</option>
          </select>
        </div>

        <div style="display:flex; align-items:flex-end;">
          <button class="btn primary" type="submit" style="width:100%;">Добавить сеть</button>
        </div>
      </form>
    </div>
    """

    edit_form = ""
    if edit_row:
        current_active = "1" if int(r(edit_row, "is_active", 0)) == 1 else "0"
        edit_form = f"""
        <div class="toolbar" style="margin-bottom:16px; padding:16px; border:1px solid #dbe2ea; border-radius:14px; background:#f8fafc;">
          <div style="font-size:18px; font-weight:700; margin-bottom:12px;">Редактирование сети ID {int(edit_row["id"])}</div>
          <form method="post" action="/admin/networks/update" style="display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:12px; width:100%;">
            <input type="hidden" name="network_id" value="{int(edit_row["id"])}">

            <div>
              <label style="display:block; margin-bottom:6px; font-weight:600;">Объект</label>
              <input type="text" name="hotel_name" value="{escape(str(r(edit_row, 'hotel_name')))}" required>
            </div>

            <div>
              <label style="display:block; margin-bottom:6px; font-weight:600;">Wi-Fi сеть</label>
              <input type="text" name="ssid_name" value="{escape(str(edit_row['ssid_name'] or ''))}" required>
            </div>

            <div>
              <label style="display:block; margin-bottom:6px; font-weight:600;">VLAN</label>
              <input type="text" name="vlan_id" value="{escape(str(edit_row['vlan_id'] or ''))}">
            </div>

            <div>
              <label style="display:block; margin-bottom:6px; font-weight:600;">Подсеть</label>
              <input type="text" name="subnet_cidr" value="{escape(str(edit_row['subnet_cidr'] or ''))}" required>
            </div>

            <div>
              <label style="display:block; margin-bottom:6px; font-weight:600;">Интерфейс MikroTik</label>
              <input type="text" name="mikrotik_interface" value="{escape(str(edit_row['mikrotik_interface'] or ''))}">
            </div>

            <div>
              <label style="display:block; margin-bottom:6px; font-weight:600;">Hotspot server</label>
              <input type="text" name="hotspot_server" value="{escape(str(edit_row['hotspot_server'] or ''))}">
            </div>

            <div>
              <label style="display:block; margin-bottom:6px; font-weight:600;">Активна</label>
              <select name="is_active" style="height:42px;">
                <option value="1" {"selected" if current_active == "1" else ""}>Да</option>
                <option value="0" {"selected" if current_active == "0" else ""}>Нет</option>
              </select>
            </div>

            <div style="display:flex; align-items:flex-end; gap:10px;">
              <button class="btn primary" type="submit">Сохранить</button>
              <a class="btn" href="/admin/networks">Отмена</a>
            </div>
          </form>
        </div>
        """

    table_html = """
    <div style="overflow-x:auto;">
      <table class="table">
        <thead>
          <tr>
            <th>ID</th>
            <th>Объект</th>
            <th>Wi-Fi сеть</th>
            <th>VLAN</th>
            <th>Подсеть</th>
            <th>Интерфейс MikroTik</th>
            <th>Hotspot server</th>
            <th>Активна</th>
            <th>Действия</th>
          </tr>
        </thead>
        <tbody>
    """

    for row in rows:
        rid = int(row["id"])
        is_active = int(r(row, "is_active", 0))
        active_text = "Да" if is_active == 1 else "Нет"
        toggle_text = "Выключить" if is_active == 1 else "Включить"

        table_html += f"""
          <tr>
            <td>{rid}</td>
            <td>{escape(str(r(row, "hotel_name")))}</td>
            <td>{escape(str(r(row, "ssid_name")))}</td>
            <td>{escape(str(r(row, "vlan_id")))}</td>
            <td>{escape(str(r(row, "subnet_cidr")))}</td>
            <td>{escape(str(r(row, "mikrotik_interface")))}</td>
            <td>{escape(str(r(row, "hotspot_server")))}</td>
            <td>{active_text}</td>
            <td>
              <div style="display:flex; gap:8px; flex-wrap:wrap;">
                <a class="btn" href="/admin/networks?edit_id={rid}">Редактировать</a>
                <form method="post" action="/admin/networks/toggle" style="margin:0;">
                  <input type="hidden" name="network_id" value="{rid}">
                  <button class="btn" type="submit">{toggle_text}</button>
                </form>
              </div>
            </td>
          </tr>
        """

    table_html += """
        </tbody>
      </table>
    </div>
    """

    body = msg_html + add_form + edit_form + table_html
    return admin_page("Сети", body, active_tab="networks")


@app.post("/admin/networks/add")
def admin_networks_add(
    request: Request,
    hotel_name: str = Form(""),
    ssid_name: str = Form(""),
    vlan_id: str = Form(""),
    subnet_cidr: str = Form(""),
    mikrotik_interface: str = Form(""),
    hotspot_server: str = Form(""),
    is_active: str = Form("1"),
):
    guard = admin_guard(request)
    if guard:
        return guard

    hotel_name = hotel_name.strip()
    ssid_name = ssid_name.strip()
    vlan_id = vlan_id.strip()
    subnet_cidr = subnet_cidr.strip()
    mikrotik_interface = mikrotik_interface.strip()
    hotspot_server = hotspot_server.strip()
    is_active_val = 1 if str(is_active).strip() == "1" else 0

    if not hotel_name:
        return RedirectResponse(url="/admin/networks?error=Не заполнено поле 'Объект'", status_code=303)

    if not ssid_name:
        return RedirectResponse(url="/admin/networks?error=Не заполнено поле 'Wi-Fi сеть'", status_code=303)

    if not subnet_cidr:
        return RedirectResponse(url="/admin/networks?error=Не заполнено поле 'Подсеть'", status_code=303)

    if vlan_id and not vlan_id.isdigit():
        return RedirectResponse(url="/admin/networks?error=VLAN должен быть числом", status_code=303)

    try:
        ipaddress.ip_network(subnet_cidr, strict=False)
    except ValueError:
        return RedirectResponse(url="/admin/networks?error=Некорректная подсеть CIDR", status_code=303)

    dup = fetch_one("""
        SELECT id
        FROM network_map
        WHERE hotel_name = ?
          AND ssid_name = ?
          AND subnet_cidr = ?
        LIMIT 1
    """, (hotel_name, ssid_name, subnet_cidr))

    if dup:
        return RedirectResponse(url="/admin/networks?error=Такая сеть уже существует", status_code=303)

    conn = db()
    conn.execute("""
        INSERT INTO network_map
        (hotel_name, ssid_name, vlan_id, subnet_cidr, mikrotik_interface, hotspot_server, is_active)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        hotel_name,
        ssid_name,
        vlan_id or None,
        subnet_cidr,
        mikrotik_interface or None,
        hotspot_server or None,
        is_active_val
    ))
    conn.commit()
    conn.close()

    return RedirectResponse(url="/admin/networks?ok=Сеть добавлена", status_code=303)


@app.post("/admin/networks/update")
def admin_networks_update(
    request: Request,
    network_id: str = Form(""),
    hotel_name: str = Form(""),
    ssid_name: str = Form(""),
    vlan_id: str = Form(""),
    subnet_cidr: str = Form(""),
    mikrotik_interface: str = Form(""),
    hotspot_server: str = Form(""),
    is_active: str = Form("1"),
):
    guard = admin_guard(request)
    if guard:
        return guard

    if not str(network_id).strip().isdigit():
        return RedirectResponse(url="/admin/networks?error=Некорректный ID сети", status_code=303)

    network_id_int = int(network_id)
    hotel_name = hotel_name.strip()
    ssid_name = ssid_name.strip()
    vlan_id = vlan_id.strip()
    subnet_cidr = subnet_cidr.strip()
    mikrotik_interface = mikrotik_interface.strip()
    hotspot_server = hotspot_server.strip()
    is_active_val = 1 if str(is_active).strip() == "1" else 0

    if not hotel_name:
        return RedirectResponse(url=f"/admin/networks?edit_id={network_id_int}&error=Не заполнено поле 'Объект'", status_code=303)

    if not ssid_name:
        return RedirectResponse(url=f"/admin/networks?edit_id={network_id_int}&error=Не заполнено поле 'Wi-Fi сеть'", status_code=303)

    if not subnet_cidr:
        return RedirectResponse(url=f"/admin/networks?edit_id={network_id_int}&error=Не заполнено поле 'Подсеть'", status_code=303)

    if vlan_id and not vlan_id.isdigit():
        return RedirectResponse(url=f"/admin/networks?edit_id={network_id_int}&error=VLAN должен быть числом", status_code=303)

    try:
        ipaddress.ip_network(subnet_cidr, strict=False)
    except ValueError:
        return RedirectResponse(url=f"/admin/networks?edit_id={network_id_int}&error=Некорректная подсеть CIDR", status_code=303)

    row = fetch_one("SELECT id FROM network_map WHERE id = ?", (network_id_int,))
    if not row:
        return RedirectResponse(url="/admin/networks?error=Сеть не найдена", status_code=303)

    dup = fetch_one("""
        SELECT id
        FROM network_map
        WHERE hotel_name = ?
          AND ssid_name = ?
          AND subnet_cidr = ?
          AND id <> ?
        LIMIT 1
    """, (hotel_name, ssid_name, subnet_cidr, network_id_int))

    if dup:
        return RedirectResponse(url=f"/admin/networks?edit_id={network_id_int}&error=Такая сеть уже существует", status_code=303)

    conn = db()
    conn.execute("""
        UPDATE network_map
        SET hotel_name = ?,
            ssid_name = ?,
            vlan_id = ?,
            subnet_cidr = ?,
            mikrotik_interface = ?,
            hotspot_server = ?,
            is_active = ?
        WHERE id = ?
    """, (
        hotel_name,
        ssid_name,
        vlan_id or None,
        subnet_cidr,
        mikrotik_interface or None,
        hotspot_server or None,
        is_active_val,
        network_id_int,
    ))
    conn.commit()
    conn.close()

    return RedirectResponse(url="/admin/networks?ok=Сеть сохранена", status_code=303)


@app.post("/admin/networks/toggle")
def admin_networks_toggle(
    request: Request,
    network_id: str = Form(""),
):
    guard = admin_guard(request)
    if guard:
        return guard

    if not str(network_id).strip().isdigit():
        return RedirectResponse(url="/admin/networks?error=Некорректный ID сети", status_code=303)

    network_id_int = int(network_id)
    row = fetch_one("SELECT id, is_active FROM network_map WHERE id = ?", (network_id_int,))
    if not row:
        return RedirectResponse(url="/admin/networks?error=Сеть не найдена", status_code=303)

    new_active = 0 if int(row["is_active"] or 0) == 1 else 1

    conn = db()
    conn.execute("UPDATE network_map SET is_active = ? WHERE id = ?", (new_active, network_id_int))
    conn.commit()
    conn.close()

    msg = "Сеть включена" if new_active == 1 else "Сеть выключена"
    return RedirectResponse(url=f"/admin/networks?ok={msg}", status_code=303)


@app.get("/admin/find", response_class=HTMLResponse)
def admin_find(request: Request, q: str = ""):
    guard = admin_guard(request)
    if guard:
        return guard

    form = f"""
    <div class="toolbar">
      <form method="get" action="/admin/find" style="display:flex; gap:10px; flex-wrap:wrap;">
        <input type="text" name="q" value="{escape(q)}" placeholder="Номер, MAC, IP или session ID">
        <button class="btn primary" type="submit">Найти</button>
      </form>

      <form method="get" action="/admin/client" style="display:flex; gap:10px; flex-wrap:wrap;">
        <input type="text" name="phone" value="{escape(q)}" placeholder="Номер телефона">
        <button class="btn primary" type="submit">Открыть карточку</button>
      </form>
    </div>
    """

    if not q.strip():
        return admin_page(
            "Поиск",
            form + '<div class="muted">Введите номер, MAC, IP или session ID.</div>',
            active_tab="find"
        )

    raw_q = q.strip()
    digits_q = re.sub(r"\D", "", raw_q)

    normalized_phone = None
    if len(digits_q) == 10:
        normalized_phone = "7" + digits_q
    elif len(digits_q) == 11 and digits_q.startswith("8"):
        normalized_phone = "7" + digits_q[1:]
    elif len(digits_q) == 11 and digits_q.startswith("7"):
        normalized_phone = digits_q

    patterns = [f"%{raw_q}%"]
    if digits_q:
        patterns.append(f"%{digits_q}%")
    if normalized_phone:
        patterns.append(f"%{normalized_phone}%")

    patterns = list(dict.fromkeys(patterns))

    session_rows = []
    call_rows = []
    audit_rows = []

    for pattern in patterns:
        session_rows.extend(fetch_all("""
            SELECT * FROM guest_sessions
            WHERE phone LIKE ?
               OR mac LIKE ?
               OR ip LIKE ?
               OR acct_session_id LIKE ?
               OR nas_id LIKE ?
            ORDER BY started_at DESC
            LIMIT 300
        """, (pattern, pattern, pattern, pattern, pattern)))

        call_rows.extend(fetch_all("""
            SELECT * FROM call_events
            WHERE phone LIKE ?
               OR callerid_raw LIKE ?
               OR source_ip LIKE ?
            ORDER BY created_at DESC
            LIMIT 300
        """, (pattern, pattern, pattern)))

        audit_rows.extend(fetch_all("""
            SELECT * FROM audit_log
            WHERE phone LIKE ?
               OR mac LIKE ?
               OR ip LIKE ?
               OR details LIKE ?
               OR nas_id LIKE ?
            ORDER BY event_time DESC
            LIMIT 300
        """, (pattern, pattern, pattern, pattern, pattern)))

    def dedupe(rows):
        seen = set()
        result = []
        for row in rows:
            if row["id"] not in seen:
                seen.add(row["id"])
                result.append(row)
        return result

    session_rows = dedupe(session_rows)
    call_rows = dedupe(call_rows)
    audit_rows = dedupe(audit_rows)

    body = form

    body += "<h2 style='margin:18px 0 12px; font-size:22px;'>Сессии</h2>"
    if session_rows:
        body += html_table(
            session_rows[:300],
            [
                "guest_id",
                "phone",
                "mac",
                "ip",
                "started_at",
                "last_seen_at",
                "ended_at",
                "status",
                "terminate_cause",
                "acct_session_time",
                "hotel",
                "ssid",
                "vlan_id",
                "nas_id",
                "acct_session_id",
            ]
        )
    else:
        body += "<div class='muted' style='margin-bottom:16px;'>Ничего не найдено.</div>"

    body += "<h2 style='margin:24px 0 12px; font-size:22px;'>Звонки</h2>"
    if call_rows:
        body += html_table(
            call_rows[:300],
            ["id", "phone", "callerid_raw", "source_ip", "created_at", "result"]
        )
    else:
        body += "<div class='muted' style='margin-bottom:16px;'>Ничего не найдено.</div>"

    body += "<h2 style='margin:24px 0 12px; font-size:22px;'>События аудита</h2>"
    if audit_rows:
        body += html_table(
            audit_rows[:300],
            ["id", "phone", "mac", "ip", "nas_id", "hotel", "ssid", "vlan_id", "event_type", "event_time", "details"]
        )
    else:
        body += "<div class='muted'>Ничего не найдено.</div>"

    return admin_page("Поиск", body, active_tab="find")    

@app.get("/admin/export/xlsx/{table_name}")
def admin_export_xlsx(
    request: Request,
    table_name: str,
    date_from: str | None = None,
    date_to: str | None = None
):
    guard = admin_guard(request)
    if guard:
        return guard

    data = build_single_xlsx(table_name, date_from=date_from, date_to=date_to)
    filename = f"{table_name}_{date_from or 'all'}_{date_to or 'all'}.xlsx"

    return StreamingResponse(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

@app.get("/admin/export", response_class=HTMLResponse)
def admin_export_page(request: Request):
    guard = admin_guard(request)
    if guard:
        return guard

    body = """
    <div class="toolbar">
      <form method="get" action="/admin/export/download" style="display:flex; flex-wrap:wrap; gap:12px; align-items:end;">
        <div>
          <label style="display:block; margin-bottom:6px; font-weight:600;">Что выгружать</label>
          <select name="table_name" style="height:42px; padding:0 12px; border:1px solid #cbd5e1; border-radius:10px; background:#fff; min-width:220px;">
            <option value="all">Все таблицы</option>
            <option value="guests">Гости</option>
            <option value="sessions">Сессии</option>
            <option value="pending">Pending</option>
            <option value="calls">Звонки</option>
            <option value="audit">Аудит</option>
            <option value="networks">Сети</option>
          </select>
        </div>

        <div>
          <label style="display:block; margin-bottom:6px; font-weight:600;">Формат</label>
          <select name="fmt" style="height:42px; padding:0 12px; border:1px solid #cbd5e1; border-radius:10px; background:#fff; min-width:160px;">
            <option value="zip">ZIP (CSV)</option>
            <option value="xlsx">XLSX</option>
          </select>
        </div>

        <div>
          <label style="display:block; margin-bottom:6px; font-weight:600;">Дата с</label>
          <input type="date" name="date_from">
        </div>

        <div>
          <label style="display:block; margin-bottom:6px; font-weight:600;">Дата по</label>
          <input type="date" name="date_to">
        </div>

        <div>
          <button class="btn primary" type="submit">Скачать</button>
        </div>
      </form>
    </div>

    <div class="muted">
      Если выбран формат XLSX, выгружается одна таблица. Для полной выгрузки всех таблиц используйте ZIP.
    </div>
    """
    return admin_page("Выгрузка", body, active_tab="export")


@app.get("/admin/export/download")
def admin_export_download(
    request: Request,
    table_name: str = "all",
    fmt: str = "zip",
    date_from: str | None = None,
    date_to: str | None = None
):
    guard = admin_guard(request)
    if guard:
        return guard

    if fmt == "zip":
        data = build_export_zip(date_from=date_from, date_to=date_to)
        filename = f"miracleon_export_{date_from or 'start'}_{date_to or 'end'}.zip"
        return StreamingResponse(
            data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )

    if fmt == "xlsx":
        if table_name == "all":
            raise HTTPException(status_code=400, detail="XLSX export supports one table only")

        data = build_single_xlsx(table_name, date_from=date_from, date_to=date_to)
        filename = f"{table_name}_{date_from or 'all'}_{date_to or 'all'}.xlsx"
        return StreamingResponse(
            data,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )

    raise HTTPException(status_code=400, detail="unknown format")


@app.get("/admin/dashboard-data")
def admin_dashboard_data(request: Request, period: str = "1d"):
    guard = admin_guard(request)
    if guard:
        return guard

    guests_cnt = fetch_all("SELECT COUNT(*) AS cnt FROM guests")[0]["cnt"]
    sessions_cnt = fetch_all("SELECT COUNT(*) AS cnt FROM guest_sessions WHERE status='active' AND ended_at IS NULL")[0]["cnt"]
    pending_cnt = fetch_all("SELECT COUNT(*) AS cnt FROM pending_auth WHERE status='pending'")[0]["cnt"]
    calls_today = fetch_all("SELECT COUNT(*) AS cnt FROM call_events WHERE date(created_at)=date('now')")[0]["cnt"]

    now_local = datetime.now(DISPLAY_TZ)

    labels = []
    values = []

    if period == "1h":
        current = now_local.replace(second=0, microsecond=0)
        minute_floor = current.minute - (current.minute % 5)
        end = current.replace(minute=minute_floor)
        start = end - timedelta(minutes=55)

        bucket_map = OrderedDict()
        for i in range(12):
            dt = start + timedelta(minutes=i * 5)
            key = dt.strftime("%Y-%m-%d %H:%M")
            bucket_map[key] = 0

        rows = fetch_all("""
            SELECT started_at
            FROM guest_sessions
            WHERE datetime(started_at) >= datetime('now', '-1 hour')
        """)

        for row in rows:
            try:
                dt = datetime.fromisoformat(row["started_at"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                dt = dt.astimezone(DISPLAY_TZ).replace(second=0, microsecond=0)
                minute_floor = dt.minute - (dt.minute % 5)
                dt = dt.replace(minute=minute_floor)
                key = dt.strftime("%Y-%m-%d %H:%M")
                if key in bucket_map:
                    bucket_map[key] += 1
            except Exception:
                continue

        labels = [datetime.strptime(k, "%Y-%m-%d %H:%M").strftime("%H:%M") for k in bucket_map.keys()]
        values = list(bucket_map.values())

    elif period == "1d":
        start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

        bucket_map = OrderedDict()
        for h in range(24):
            dt = start + timedelta(hours=h)
            key = dt.strftime("%Y-%m-%d %H")
            bucket_map[key] = 0

        rows = fetch_all("""
            SELECT started_at
            FROM guest_sessions
            WHERE date(datetime(started_at, '+3 hours')) = date(datetime('now', '+3 hours'))
        """)

        for row in rows:
            try:
                dt = datetime.fromisoformat(row["started_at"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                dt = dt.astimezone(DISPLAY_TZ).replace(minute=0, second=0, microsecond=0)
                key = dt.strftime("%Y-%m-%d %H")
                if key in bucket_map:
                    bucket_map[key] += 1
            except Exception:
                continue

        labels = [f"{h:02d}:00" for h in range(24)]
        values = list(bucket_map.values())

    elif period == "1mo":
        start_day = now_local.date() - timedelta(days=29)

        bucket_map = OrderedDict()
        for i in range(30):
            d = start_day + timedelta(days=i)
            key = d.strftime("%Y-%m-%d")
            bucket_map[key] = 0

        rows = fetch_all("""
            SELECT started_at
            FROM guest_sessions
            WHERE datetime(started_at) >= datetime('now', '-30 days')
        """)

        for row in rows:
            try:
                dt = datetime.fromisoformat(row["started_at"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                dt = dt.astimezone(DISPLAY_TZ).date()
                key = dt.strftime("%Y-%m-%d")
                if key in bucket_map:
                    bucket_map[key] += 1
            except Exception:
                continue

        labels = [datetime.strptime(k, "%Y-%m-%d").strftime("%d.%m") for k in bucket_map.keys()]
        values = list(bucket_map.values())

    elif period == "1y":
        bucket_map = OrderedDict()
        year = now_local.year
        month = now_local.month

        months = []
        for i in range(11, -1, -1):
            y = year
            m = month - i
            while m <= 0:
                m += 12
                y -= 1
            months.append((y, m))

        for y, m in months:
            key = f"{y:04d}-{m:02d}"
            bucket_map[key] = 0

        rows = fetch_all("""
            SELECT started_at
            FROM guest_sessions
            WHERE datetime(started_at) >= datetime('now', '-1 year')
        """)

        for row in rows:
            try:
                dt = datetime.fromisoformat(row["started_at"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                dt = dt.astimezone(DISPLAY_TZ)
                key = dt.strftime("%Y-%m")
                if key in bucket_map:
                    bucket_map[key] += 1
            except Exception:
                continue

        labels = [datetime.strptime(k, "%Y-%m").strftime("%m.%Y") for k in bucket_map.keys()]
        values = list(bucket_map.values())

    else:
        period = "1d"
        start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

        bucket_map = OrderedDict()
        for h in range(24):
            dt = start + timedelta(hours=h)
            key = dt.strftime("%Y-%m-%d %H")
            bucket_map[key] = 0

        rows = fetch_all("""
            SELECT started_at
            FROM guest_sessions
            WHERE date(datetime(started_at, '+3 hours')) = date(datetime('now', '+3 hours'))
        """)

        for row in rows:
            try:
                dt = datetime.fromisoformat(row["started_at"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                dt = dt.astimezone(DISPLAY_TZ).replace(minute=0, second=0, microsecond=0)
                key = dt.strftime("%Y-%m-%d %H")
                if key in bucket_map:
                    bucket_map[key] += 1
            except Exception:
                continue

        labels = [f"{h:02d}:00" for h in range(24)]
        values = list(bucket_map.values())

    return {
        "stats": {
            "guests": guests_cnt,
            "active_sessions": sessions_cnt,
            "pending": pending_cnt,
            "calls_today": calls_today
        },
        "chart": {
            "labels": labels,
            "values": values,
            "period": period
        }
    }


@app.get("/admin/client", response_class=HTMLResponse)
def admin_client(request: Request, phone: str = ""):
    guard = admin_guard(request)
    if guard:
        return guard

    if not phone.strip():
        return RedirectResponse(url="/admin/find", status_code=302)

    try:
        normalized_phone = normalize_phone(phone)
    except ValueError:
        body = f"""
        <div class="toolbar">
          <form method="get" action="/admin/client" style="display:flex; gap:10px; flex-wrap:wrap;">
            <input type="text" name="phone" value="{escape(phone)}" placeholder="Введите номер телефона">
            <button class="btn primary" type="submit">Открыть карточку</button>
          </form>
        </div>
        <div class="muted">Неверный формат номера.</div>
        """
        return admin_page("Карточка клиента", body, active_tab="find")

    guest = fetch_one("""
        SELECT *
        FROM guests
        WHERE phone = ?
        LIMIT 1
    """, (normalized_phone,))

    sessions = fetch_all("""
        SELECT *
        FROM guest_sessions
        WHERE phone = ?
        ORDER BY CASE WHEN status='active' THEN 0 ELSE 1 END, started_at DESC
        LIMIT 10
    """, (normalized_phone,))

    calls = fetch_all("""
        SELECT *
        FROM call_events
        WHERE phone = ?
        ORDER BY created_at DESC
        LIMIT 10
    """, (normalized_phone,))

    audit_rows = fetch_all("""
        SELECT *
        FROM audit_log
        WHERE phone = ?
        ORDER BY event_time DESC
        LIMIT 20
    """, (normalized_phone,))

    stats = fetch_one("""
        SELECT
            COUNT(*) AS total_sessions,
            SUM(CASE WHEN status='active' AND ended_at IS NULL THEN 1 ELSE 0 END) AS active_sessions,
            MAX(started_at) AS last_session_start,
            MAX(last_seen_at) AS last_seen_at,
            MAX(ended_at) AS last_ended_at
        FROM guest_sessions
        WHERE phone = ?
    """, (normalized_phone,))

    last_closed = fetch_one("""
        SELECT *
        FROM guest_sessions
        WHERE phone = ?
          AND status = 'closed'
        ORDER BY ended_at DESC
        LIMIT 1
    """, (normalized_phone,))

    active_now = "Нет"
    if stats and stats["active_sessions"] and int(stats["active_sessions"]) > 0:
        active_now = "Да"

    summary_cards = f"""
    <div class="toolbar">
      <form method="get" action="/admin/client" style="display:flex; gap:10px; flex-wrap:wrap;">
        <input type="text" name="phone" value="{escape(normalized_phone)}" placeholder="Введите номер телефона">
        <button class="btn primary" type="submit">Обновить</button>
      </form>
    </div>

    <div class="stats">
      <div class="stat">
        <div class="stat-label">Номер</div>
        <div class="stat-value" style="font-size:20px;">{escape(normalized_phone)}</div>
      </div>
      <div class="stat">
        <div class="stat-label">ID гостя</div>
        <div class="stat-value">{guest['id'] if guest else '—'}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Активен сейчас</div>
        <div class="stat-value">{active_now}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Всего сессий</div>
        <div class="stat-value">{stats['total_sessions'] if stats and stats['total_sessions'] is not None else 0}</div>
      </div>
    </div>

    <div class="stats">
      <div class="stat">
        <div class="stat-label">Первая верификация</div>
        <div class="stat-value" style="font-size:18px;">{format_dt(guest['first_verified_at']) if guest and guest['first_verified_at'] else '—'}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Последний допуск</div>
        <div class="stat-value" style="font-size:18px;">{format_dt(guest['last_auth_at']) if guest and guest['last_auth_at'] else '—'}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Последняя активность</div>
        <div class="stat-value" style="font-size:18px;">{format_dt(stats['last_seen_at']) if stats and stats['last_seen_at'] else '—'}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Последняя завершённая сессия</div>
        <div class="stat-value" style="font-size:18px;">{format_dt(last_closed['ended_at']) if last_closed and last_closed['ended_at'] else '—'}</div>
      </div>
    </div>
    """

    body = summary_cards

    body += "<h2 style='margin:24px 0 12px; font-size:22px;'>Гость</h2>"
    if guest:
        body += html_table([guest], ["id", "phone", "first_verified_at", "first_hotel", "auth_method", "status", "created_at", "updated_at", "last_auth_at"])
    else:
        body += "<div class='muted' style='margin-bottom:16px;'>Запись гостя не найдена.</div>"

    body += "<h2 style='margin:24px 0 12px; font-size:22px;'>Последние сессии</h2>"
    if sessions:
        body += html_table(
            sessions,
            [
                "guest_id",
                "phone",
                "mac",
                "ip",
                "device_name",
                "started_at",
                "last_seen_at",
                "ended_at",
                "status",
                "terminate_cause",
                "acct_session_time",
                "hotel",
                "ssid",
                "vlan_id",
                "nas_id",
                "acct_session_id",
            ]
        )
    else:
        body += "<div class='muted' style='margin-bottom:16px;'>Сессий не найдено.</div>"

    body += "<h2 style='margin:24px 0 12px; font-size:22px;'>Последние звонки</h2>"
    if calls:
        body += html_table(calls, ["id", "phone", "callerid_raw", "source_ip", "created_at", "result"])
    else:
        body += "<div class='muted' style='margin-bottom:16px;'>Звонков не найдено.</div>"

    body += "<h2 style='margin:24px 0 12px; font-size:22px;'>Последние события аудита</h2>"
    if audit_rows:
        body += html_table(audit_rows, ["id", "phone", "mac", "ip", "nas_id", "hotel", "ssid", "vlan_id", "event_type", "event_time", "details"])
    else:
        body += "<div class='muted'>Событий не найдено.</div>"

    return admin_page("Карточка клиента", body, active_tab="find")






