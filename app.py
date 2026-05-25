from fastapi import FastAPI, HTTPException, Query, Request, Form, Body
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
from html import escape
from collections import OrderedDict
import re
import threading
import ipaddress
from urllib.parse import quote
from fastapi.staticfiles import StaticFiles
import subprocess
import sqlite3
import select
import os

import logging

from system_info import (
    get_disk_usage,
    get_memory_usage,
    get_cpu_load,
    get_uptime,
    disk_status_class,
    human_bytes,
)

from app_services.settings_store import (
    get_setting,
    set_setting,
    init_settings_table,
)

from app_services.onec_store import (
    init_onec_sites_table,
    list_onec_sites,
    upsert_onec_site,
    delete_onec_site,
    get_onec_site_by_code,
)

from app_services.opera_store import (
    init_opera_sites_table,
    list_opera_sites,
    upsert_opera_site,
    delete_opera_site,
)


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
    ensure_guests_auth_columns,
    ensure_guest_sessions_device_name,
    ensure_guest_sessions_room_auth_columns,
    ensure_terminate_cause_columns,
)

from exports import (
    rows_to_xlsx_bytes,
    build_single_xlsx,
    build_export_zip,
)

from integrations.mikrotik.api import (
    fetch_dhcp_leases,
    mt_api,
    disconnect_hotspot_active_by_mac,
    fetch_hotspot_active,
)

from config import (
    APP_NAME,
    APP_SECRET,
    DB_PATH,
    OPERA_CACHE_DB_PATH,
    ADMIN_USERNAME,
    ADMIN_PASSWORD,
    ADMIN_COOKIE,
    DEVICE_LIMIT,
    PENDING_MINUTES,
    PBX_ALLOWED_IPS,
)

from api_security import ip_allowed, require_api_guard

from admin_auth import (
    make_admin_token,
    ADMIN_SESSION_TTL_SECONDS,
    admin_guard,
    role_guard,
    get_current_admin_user,
    ensure_admin_users_table,
    bootstrap_admin_users,
    verify_admin_password,
    hash_admin_password,
)


from services import (
    DISPLAY_TZ,
    normalize_accounting_event_time,
    resolve_network_info,
    audit,
    save_radius_accounting,
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
    get_or_create_room_guest,
    make_room_identity,
    get_recent_authorized_session_by_mac,
)

from ui import (
    format_dt,
    html_table,
    admin_page,
)

from integrations.opera.lookup import normalize_user_surname
from room_auth import ensure_room_auth_table, save_verified_room_auth, get_verified_room_auth
from integrations.pms.router import pms_room_auth_allowed

from vouchers import (
    ensure_voucher_tables,
    create_voucher,
    verify_voucher_radius,
    format_voucher_code,
    decrypt_voucher_code,
)

from logging_config import setup_logging

setup_logging()
logger = logging.getLogger(__name__)
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RADIUS_ALLOWED_IPS = ("127.0.0.1", "::1")


def split_csv_items(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]

    return [
        item.strip()
        for item in str(value or "").split(",")
        if item.strip()
    ]


def csv_setting_items(key: str, default_items: tuple[str, ...] | list[str] = ()) -> list[str]:
    return split_csv_items(get_setting(key, ",".join(default_items)))


def ensure_security_default_settings() -> None:
    if get_setting("radius.allowed_ips", None) is None:
        set_setting("radius.allowed_ips", ",".join(DEFAULT_RADIUS_ALLOWED_IPS))


def mask_phone(phone: str | None) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    return digits or "-"

def log_phone_event(event: str, phone: str | None = None, **fields):
    safe_fields = {
        key: value
        for key, value in fields.items()
        if value not in (None, "")
    }

    parts = [f"phone={mask_phone(phone)}"]
    parts.extend(f"{key}={value}" for key, value in safe_fields.items())

    logger.info("%s %s", event, " ".join(parts))


app = FastAPI()
ensure_room_auth_table()
app.mount("/static", StaticFiles(directory="static"), name="static")


class CallIn(BaseModel):
    phone: str


class RadiusCheckIn(BaseModel):
    username: str
    password: str | None = None
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


@app.get("/admin/system/disk")
def admin_system_disk(request: Request):
    guard = role_guard(request, ("admin", "superadmin", "it"))
    if guard:
        return guard

    return get_disk_usage("/")


def systemctl_is_active(unit: str) -> str:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return (r.stdout or "").strip() or "unknown"
    except Exception:
        return "unknown"


def service_badge(status: str) -> str:
    if status == "active":
        return '<span class="badge active">active</span>'
    if status in ("inactive", "failed"):
        return f'<span class="badge error">{escape(status)}</span>'
    return f'<span class="badge pending">{escape(status)}</span>'


def readiness_badge(status: str) -> str:
    labels = {
        "ok": ("active", "ok"),
        "warn": ("pending", "attention"),
        "bad": ("error", "problem"),
    }
    cls, label = labels.get(status, ("pending", status or "unknown"))
    return f'<span class="badge {cls}">{escape(label)}</span>'


def get_wal_size() -> dict:
    path = DB_PATH + "-wal"
    if not os.path.exists(path):
        return {"bytes": 0, "text": "0 B"}

    size = os.path.getsize(path)
    return {"bytes": size, "text": human_bytes(size)}


def udp_port_listening(port: int) -> bool:
    try:
        r = subprocess.run(
            ["ss", "-lun"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return f":{port} " in r.stdout or f":{port}\n" in r.stdout
    except Exception:
        return False


def get_network_counts() -> dict:
    try:
        row = fetch_one("""
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END), 0) AS active
            FROM network_map
        """)
        return {
            "total": int(row["total"] or 0) if row else 0,
            "active": int(row["active"] or 0) if row else 0,
        }
    except Exception:
        return {"total": 0, "active": 0}


def get_last_radius_event() -> dict:
    try:
        row = fetch_one("""
            SELECT event_type, event_time
            FROM audit_log
            WHERE event_type LIKE 'radius_%'
               OR event_type = 'pending_created'
            ORDER BY event_time DESC
            LIMIT 1
        """)
        if not row:
            return {"event_type": "", "event_time": ""}
        return {
            "event_type": str(row["event_type"] or ""),
            "event_time": str(row["event_time"] or ""),
        }
    except Exception:
        return {"event_type": "", "event_time": ""}


def build_readiness_rows(service_statuses: dict[str, str]) -> str:
    rows = []

    def add(title: str, status: str, details: str, href: str = "") -> None:
        title_html = escape(title)
        if href:
            title_html = f'<a href="{escape(href)}">{title_html}</a>'
        rows.append(f"""
          <tr>
            <td>{title_html}</td>
            <td>{readiness_badge(status)}</td>
            <td>{escape(details)}</td>
          </tr>
        """)

    portal_active = service_statuses.get("portal") == "active"
    radius_active = service_statuses.get("freeradius") == "active"
    radius_ports_ok = udp_port_listening(1812) and udp_port_listening(1813)
    summary_path = os.path.join(APP_DIR, "setup-summary.txt")
    networks = get_network_counts()

    mt_host = str(get_setting("mikrotik.host", "") or "").strip()
    mt_user = str(get_setting("mikrotik.user", "") or "").strip()
    mt_password = str(get_setting("mikrotik.password", "") or "").strip()

    onec_sites = list_onec_sites()
    opera_sites = list_opera_sites()
    onec_active = sum(1 for site in onec_sites if site.get("enabled"))
    opera_active = sum(1 for site in opera_sites if site.get("enabled"))

    radius_allowed_ips = csv_setting_items("radius.allowed_ips", DEFAULT_RADIUS_ALLOWED_IPS)
    pbx_enabled = str(get_setting("pbx.enabled", "1")) == "1"
    pbx_allowed_ips = csv_setting_items("pbx.allowed_ips", PBX_ALLOWED_IPS)

    last_radius = get_last_radius_event()
    opera_status = get_opera_fias_status()

    add(
        "Portal backend",
        "ok" if portal_active else "bad",
        "systemd active" if portal_active else f"systemd {service_statuses.get('portal', 'unknown')}",
    )
    add(
        "FreeRADIUS",
        "ok" if radius_active and radius_ports_ok else "bad",
        "service active, UDP 1812/1813 listening"
        if radius_active and radius_ports_ok
        else f"service {service_statuses.get('freeradius', 'unknown')}, ports {'ok' if radius_ports_ok else 'not ready'}",
    )
    add(
        "Setup summary",
        "ok" if os.path.exists(summary_path) else "warn",
        summary_path if os.path.exists(summary_path) else "setup-summary.txt not found",
    )
    add(
        "Networks",
        "ok" if networks["active"] > 0 else "warn",
        f"{networks['active']} active / {networks['total']} total",
        "/admin/system?section=networks",
    )
    add(
        "MikroTik",
        "ok" if mt_host and mt_user and mt_password else "warn",
        "host, user and password configured"
        if mt_host and mt_user and mt_password
        else "not fully configured",
        "/admin/system?section=settings",
    )
    add(
        "RADIUS API guard",
        "ok" if radius_allowed_ips else "bad",
        "allowed IPs: " + ", ".join(radius_allowed_ips)
        if radius_allowed_ips
        else "allowed IPs are empty",
        "/admin/system?section=settings",
    )
    add(
        "PBX / Asterisk",
        "ok" if pbx_enabled and pbx_allowed_ips else "warn",
        "enabled with allowed IPs"
        if pbx_enabled and pbx_allowed_ips
        else ("disabled" if not pbx_enabled else "allowed IPs are empty"),
        "/admin/system?section=settings",
    )
    add(
        "PMS objects",
        "ok" if (onec_active + opera_active) > 0 else "warn",
        f"1C active: {onec_active}, Opera active: {opera_active}",
        "/admin/system?section=settings",
    )
    add(
        "Last RADIUS request",
        "ok" if last_radius["event_time"] else "warn",
        f"{format_dt(last_radius['event_time'])} / {last_radius['event_type']}"
        if last_radius["event_time"]
        else "no radius-check events yet",
        "/admin/audit",
    )
    add(
        "Last Opera/FIAS RX",
        "ok" if opera_status.get("last_event") else "warn",
        str(opera_status.get("last_event") or "no FIAS events yet"),
        "/admin/system?section=settings",
    )

    return "".join(rows)

@app.get("/admin/system/service/status-json")
def admin_system_service_status_json(request: Request):
    guard = role_guard(request, ("superadmin", "it"))
    if guard:
        return {"ok": False, "error": "forbidden"}

    disk = get_disk_usage("/")
    memory = get_memory_usage()
    cpu = get_cpu_load()
    uptime = get_uptime()
    wal = get_wal_size()

    services = [
        ("portal", "hotspot-captive-portal.service"),
        ("cleanup", "hotspot-cleanup-worker.service"),
        ("mikrotik", "hotspot-mikrotik-sync-worker.service"),
        ("opera", "opera-fias-sync.service"),
        ("freeradius", "freeradius.service"),
    ]

    return {
        "ok": True,
        "cpu": cpu,
        "memory": memory,
        "disk": disk,
        "uptime": uptime,
        "wal": wal,
        "services": {
            key: systemctl_is_active(unit)
            for key, unit in services
        },
    }


@app.get("/")
def root():
    return RedirectResponse(url="/admin/login", status_code=302)


def get_opera_fias_status():
    status = {
        "service_active": False,
        "last_event": "",
        "event_count": 0,
        "raw_count": 0,
        "socket_established": False,
    }

    try:
        r = subprocess.run(
            ["systemctl", "is-active", "opera-fias-sync.service"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        status["service_active"] = r.stdout.strip() == "active"
    except Exception:
        pass

    opera_socket = ""

    try:
        opera_sites = list_opera_sites()
        opera_site = next((s for s in opera_sites if s.get("enabled")), None)

        if opera_site:
            opera_host = str(opera_site.get("host") or "")
            opera_port = str(opera_site.get("port") or "")
            if opera_host and opera_port:
                opera_socket = f"{opera_host}:{opera_port}"
    except Exception:
        pass

    try:
        r = subprocess.run(
            ["ss", "-tanp"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        status["socket_established"] = bool(
            opera_socket and opera_socket in r.stdout and "ESTAB" in r.stdout
        )
    except Exception:
        pass

    try:
        db = OPERA_CACHE_DB_PATH
        conn = sqlite3.connect(db)

        row = conn.execute("SELECT count(*), max(received_at) FROM opera_events").fetchone()
        if row:
            status["event_count"] = row[0] or 0
            status["last_event"] = row[1] or ""

        row = conn.execute("SELECT count(*) FROM opera_raw_events").fetchone()
        if row:
            status["raw_count"] = row[0] or 0

        conn.close()
    except Exception:
        pass

    return status


def pms_api_guard(request: Request) -> None:
    enabled = str(get_setting("pms_api.enabled", "0")) == "1"
    if not enabled:
        return

    token = str(get_setting("pms_api.token", "") or "").strip()
    allowed_ips = csv_setting_items("pms_api.allowed_ips")

    require_api_guard(
        request,
        token=token,
        allowed_ips=allowed_ips,
        not_configured_detail="pms_api_guard_not_configured",
    )


def radius_api_guard(request: Request) -> None:
    client_ip = request.client.host if request.client else ""
    allowed_ips = csv_setting_items("radius.allowed_ips", DEFAULT_RADIUS_ALLOWED_IPS)

    try:
        require_api_guard(
            request,
            allowed_ips=allowed_ips,
            not_configured_detail="radius_guard_not_configured",
        )
    except HTTPException as exc:
        audit(
            "radius_api_forbidden",
            ip=client_ip,
            details=f"RADIUS API rejected: {exc.detail}",
        )
        raise


def pbx_api_guard(request: Request) -> None:
    client_ip = request.client.host if request.client else ""
    pbx_enabled = str(get_setting("pbx.enabled", "1")) == "1"
    pbx_allowed_ips = csv_setting_items("pbx.allowed_ips", PBX_ALLOWED_IPS)

    if not pbx_enabled:
        audit("pbx_disabled", ip=client_ip, details="PBX call rejected because PBX is disabled")
        raise HTTPException(status_code=403, detail="pbx disabled")

    if not pbx_allowed_ips:
        audit("pbx_not_configured", ip=client_ip, details="PBX call rejected because allowed IPs are empty")
        raise HTTPException(status_code=403, detail="pbx_guard_not_configured")

    if not ip_allowed(client_ip, pbx_allowed_ips):
        audit("pbx_forbidden_ip", ip=client_ip, details="PBX call rejected by IP allowlist")
        raise HTTPException(status_code=403, detail="pbx ip forbidden")

_ONEC_STATUS_CACHE = {}


def check_onec_site_status(site: dict) -> bool:
    if not site.get("enabled"):
        return False

    site_id = site.get("id")
    base_url = site.get("base_url") or ""
    token = site.get("token") or ""

    if not base_url or not token:
        return False

    import time
    import requests

    now = time.time()
    cache_key = str(site_id or base_url)

    cached = _ONEC_STATUS_CACHE.get(cache_key)
    if cached and now - cached["ts"] < 60:
        return cached["ok"]

    try:
        r = requests.get(
            base_url,
            params={
                "token": token,
                "room": "__healthcheck__",
                "LastName": "__healthcheck__",
            },
            timeout=2,
        )

        ok = False

        if r.status_code < 500:
            try:
                data = r.json()
                err = str(data.get("Error", "")).lower()

                bad_token_markers = [
                    "invalid token",
                    "wrong token",
                    "token disabled",
                    "access denied",
                    "unauthorized",
                    "forbidden",
                    "неверный токен",
                    "failed to find interaction parameters",
                    "interaction parameters",
                ]

                if not any(marker in err for marker in bad_token_markers):
                    ok = True
            except Exception:
                ok = False

        _ONEC_STATUS_CACHE[cache_key] = {
            "ts": now,
            "ok": ok,
        }

        return ok

    except Exception:
        _ONEC_STATUS_CACHE[cache_key] = {
            "ts": now,
            "ok": False,
        }

        return False


PMS_CHECK_ERROR_LABELS = {
    "hotel_not_resolved": "Объект не выбран",
    "pms_not_configured_for_hotel": "Для объекта не настроен включенный PMS",
    "guest_not_found": "Гость не найден",
    "1c_site_not_configured_or_disabled": "Объект 1C выключен или не настроен",
    "1c_site_missing_token_or_url": "У объекта 1C не указан URL или токен",
}


def get_pms_check_hotels() -> list[str]:
    rows = fetch_all("""
        SELECT DISTINCT hotel_name
        FROM network_map
        WHERE is_active = 1
          AND hotel_name IS NOT NULL
          AND TRIM(hotel_name) != ''
        ORDER BY lower(hotel_name)
    """)

    return [str(row["hotel_name"]) for row in rows]


def pms_check_error_label(error: str) -> str:
    error = (error or "").strip()
    return PMS_CHECK_ERROR_LABELS.get(error, error or "Нет деталей")


def build_pms_check_result_html(pms_check: dict | None) -> str:
    if not pms_check:
        return ""

    result = pms_check.get("result") or {}
    ok = bool(result.get("ok"))
    source = str(result.get("source") or "не выбран")
    error = str(result.get("error") or "")
    hotel = str(pms_check.get("hotel") or "")
    room_num = str(pms_check.get("room_num") or "")

    config_errors = {
        "hotel_not_resolved",
        "pms_not_configured_for_hotel",
        "1c_site_not_configured_or_disabled",
        "1c_site_missing_token_or_url",
    }

    if ok:
        title = "Гость найден"
        color = "rgba(22, 163, 74, .16)"
        border = "rgba(22, 163, 74, .36)"
        interface_text = "Интерфейс ответил"
        guest_text = "Портал пустит гостя по номеру и фамилии"
    elif error in config_errors:
        title = "PMS не готов к проверке"
        color = "rgba(245, 158, 11, .18)"
        border = "rgba(245, 158, 11, .38)"
        interface_text = pms_check_error_label(error)
        guest_text = "Гость не проверялся"
    else:
        title = "Гость не найден"
        color = "rgba(153, 27, 27, .16)"
        border = "rgba(248, 113, 113, .35)"
        interface_text = "Интерфейс ответил" if source != "не выбран" else "PMS не выбран"
        guest_text = pms_check_error_label(error)

    details = []
    if result.get("reservation_number"):
        details.append(f"Бронь: {escape(str(result.get('reservation_number')))}")
    if result.get("checkin_date"):
        details.append(f"Заезд: {escape(str(result.get('checkin_date')))}")
    if result.get("checkout_date"):
        details.append(f"Выезд: {escape(str(result.get('checkout_date')))}")

    details_html = ""
    if details:
        details_html = "<br>" + "<br>".join(details)

    return f"""
      <div class="notice" style="margin-top:14px; padding:14px 16px; border-radius:14px; background:{color}; border:1px solid {border};">
        <b>{escape(title)}</b><br>
        Объект: {escape(hotel or "-")}<br>
        Комната: {escape(room_num or "-")}<br>
        PMS: {escape(source)}<br>
        Интерфейс: {escape(interface_text)}<br>
        Гость: {escape(guest_text)}
        {details_html}
      </div>
    """


def build_settings_body(ok: str = "", pms_check: dict | None = None):

    mt_host = escape(str(get_setting("mikrotik.host", "")))
    mt_port = escape(str(get_setting("mikrotik.port", "8728")))
    mt_user = escape(str(get_setting("mikrotik.user", "")))
    mt_interval = escape(str(get_setting("mikrotik.device_sync_interval", "300")))

    ok_html = ""
    if ok:
        ok_html = """
        <div class="notice success" style="margin-bottom:14px;">
          Настройки сохранены.
        </div>
        """

    onec_sites = list_onec_sites()

    onec_rows = ""
    for site in onec_sites:
        site_for_check = get_onec_site_by_code(site["code"])
        status_dot = "🟢" if site_for_check and check_onec_site_status(site_for_check) else "🔴"
        enabled_text = "Да" if site["enabled"] else "Нет"

        onec_rows += f"""
        <tr>
          <td>{status_dot} {escape(site["name"])}</td>
          <td><code>{escape(site["code"])}</code></td>
          <td>{escape(site["base_url"])}</td>
          <td>{escape(site["token_masked"])}</td>
          <td>{enabled_text}</td>
          <td>{site["timeout"]}</td>
          <td>
            <form method="post" action="/admin/settings/onec/delete" style="display:inline;">
              <input type="hidden" name="site_id" value="{site["id"]}">
              <button type="submit" class="btn btn-danger"
                onclick="return confirm('Удалить объект 1C?')">Удалить</button>
            </form>
          </td>
        </tr>
        """

    if not onec_rows:
        onec_rows = """
        <tr>
          <td colspan="7" class="muted">Объекты 1C пока не добавлены.</td>
        </tr>
        """    

    opera_sites = list_opera_sites()

    opera_rows = ""
    for site in opera_sites:
        opera_status = get_opera_fias_status()
        opera_is_ok = (
            site["enabled"]
            and opera_status["service_active"]
            and opera_status["socket_established"]
        )
        status_dot = "🟢" if opera_is_ok else "🔴"
        enabled_text = "Да" if site["enabled"] else "Нет"
        ssl_text = "Да" if site["use_ssl"] else "Нет"

        opera_rows += f"""
        <tr>
          <td>{status_dot} {escape(site["name"])}</td>
          <td><code>{escape(site["code"])}</code></td>
          <td>{escape(site["host"])}</td>
          <td>{site["port"]}</td>
          <td>{ssl_text}</td>
          <td>{escape(site["property_code"])}</td>
          <td>{escape(site["auth_key_masked"])}</td>
          <td>{enabled_text}</td>
          <td>
            <form method="post" action="/admin/settings/opera/delete" style="display:inline;">
              <input type="hidden" name="site_id" value="{site["id"]}">
              <button type="submit" class="btn btn-danger"
                onclick="return confirm('Удалить Opera/FIAS объект?')">
                Удалить
              </button>
            </form>
          </td>
        </tr>
        """

    if not opera_rows:
        opera_rows = """
        <tr>
          <td colspan="9" class="muted">
            Opera / FIAS объекты пока не добавлены.
          </td>
        </tr>
        """

    opera_status = get_opera_fias_status()

    opera_status_color = "🟢" if (
        opera_status["service_active"] and opera_status["socket_established"]
    ) else "🔴"

    opera_status_html = f"""
      <div class="notice" style="margin:12px 0;">
        <b>{opera_status_color} Opera/FIAS статус</b><br>
        Service: {"active" if opera_status["service_active"] else "inactive"}<br>
        TCP link: {"ESTAB" if opera_status["socket_established"] else "нет соединения"}<br>
        Last RX: {escape(str(opera_status["last_event"] or "нет данных"))}<br>
        Events: {opera_status["event_count"]}, Raw: {opera_status["raw_count"]}
      </div>
    """

    radius_allowed_ips = str(get_setting("radius.allowed_ips", ",".join(DEFAULT_RADIUS_ALLOWED_IPS)))
    pbx_enabled = str(get_setting("pbx.enabled", "1")) == "1"
    pbx_allowed_ips = str(get_setting("pbx.allowed_ips", ",".join(PBX_ALLOWED_IPS)))

    pms_api_enabled = str(get_setting("pms_api.enabled", "0")) == "1"
    pms_allowed_ips = str(get_setting("pms_api.allowed_ips", ""))
    pms_token_is_set = bool(str(get_setting("pms_api.token", "") or "").strip())
    pms_token_placeholder = "Токен задан" if pms_token_is_set else "Новый токен"

    pms_check = pms_check or {}
    pms_check_hotel = str(pms_check.get("hotel") or "")
    pms_check_room = str(pms_check.get("room_num") or "")
    pms_check_surname = str(pms_check.get("surname") or "")
    pms_check_options = ['<option value="">Выберите объект</option>']

    for hotel_name in get_pms_check_hotels():
        selected = " selected" if hotel_name == pms_check_hotel else ""
        pms_check_options.append(
            f'<option value="{escape(hotel_name)}"{selected}>{escape(hotel_name)}</option>'
        )

    if len(pms_check_options) == 1:
        pms_check_options.append('<option value="" disabled>Активные сети не настроены</option>')

    pms_check_result_html = build_pms_check_result_html(pms_check)

    body = f"""
    <div class="settings-page">

      <div class="settings-card">
        <h2>Настройки приложения</h2>
        <p class="settings-muted">
          Рабочие параметры сохраняются в базе данных. .env остаётся техническим
          файлом установки для секретов и путей, которые создаёт install.
        </p>
        {ok_html}
      </div>

      <div class="settings-card">
        <form method="post" action="/admin/settings/mikrotik">
          <h3 style="margin:0 0 12px;">MikroTik</h3>

          <div class="settings-grid settings-grid-mikrotik">
            <div class="settings-field">
              <label>Host</label>
              <input type="text" name="host" value="{mt_host}" placeholder="192.168.88.1">
            </div>

            <div class="settings-field">
              <label>Port</label>
              <input type="number" name="port" value="{mt_port}" placeholder="8728">
            </div>

            <div class="settings-field">
              <label>User</label>
              <input type="text" name="user" value="{mt_user}" placeholder="api-user">
            </div>

            <div class="settings-field">
              <label>Password</label>
              <input type="password" name="password" value="" placeholder="Оставить пустым, чтобы не менять">
            </div>

            <div class="settings-field">
              <label>Device sync interval, sec</label>
              <input type="number" name="device_sync_interval" value="{mt_interval}" placeholder="300">
            </div>

            <div class="settings-field" style="align-self:end;">
              <button type="submit" class="btn btn-primary">Сохранить MikroTik</button>
            </div>
          </div>
        </form>
      </div>


      <div class="settings-card">
        <form method="post" action="/admin/settings/radius-api">
          <h3 style="margin:0 0 12px;">RADIUS / FreeRADIUS</h3>

          <div class="settings-grid settings-grid-pbx">
            <div class="settings-field">
              <label>Разрешённые IP / CIDR</label>
              <input type="text"
                   name="radius_allowed_ips"
                   value="{escape(radius_allowed_ips)}"
                   placeholder="127.0.0.1,::1">
            </div>

            <div class="settings-field" style="align-self:end;">
              <button type="submit" class="btn btn-primary">Сохранить RADIUS</button>
            </div>
          </div>
        </form>
      </div>


      <div class="settings-card">
        <form method="post" action="/admin/settings/pbx">
          <h3 style="margin:0 0 12px;">PBX / Asterisk</h3>

          <div class="settings-grid settings-grid-pbx">
            <div class="settings-field">
              <label>Включено</label>
              <select name="pbx_enabled">
                <option value="1" {"selected" if pbx_enabled else ""}>Да</option>
                <option value="0" {"" if pbx_enabled else "selected"}>Нет</option>
              </select>
            </div>

            <div class="settings-field">
              <label>Разрешённые IP</label>
              <input type="text"
                   name="pbx_allowed_ips"
                   value="{escape(pbx_allowed_ips)}"
                   placeholder="192.168.4.150,192.168.4.151">
            </div>

            <div class="settings-field" style="align-self:end;">
              <button type="submit" class="btn btn-primary">Сохранить PBX</button>
            </div>
          </div>
        </form>
      </div>


      <div class="settings-card">
        <form method="post" action="/admin/settings/pms-api">
          <h3 style="margin:0 0 12px;">PMS API</h3>

          <div class="settings-grid settings-grid-pbx">
            <div class="settings-field">
              <label>Защита включена</label>
              <select name="pms_api_enabled">
                <option value="0" {"" if pms_api_enabled else "selected"}>Нет</option>
                <option value="1" {"selected" if pms_api_enabled else ""}>Да</option>
              </select>
            </div>

            <div class="settings-field">
              <label>Разрешённые IP / CIDR</label>
              <input type="text"
                   name="pms_allowed_ips"
                   value="{escape(pms_allowed_ips)}"
                   placeholder="10.99.0.0/24,127.0.0.1">
            </div>

            <div class="settings-field">
              <label>Токен</label>
              <input type="password"
                   name="pms_api_token"
                   value=""
                   placeholder="{escape(pms_token_placeholder)}">
            </div>

            <div class="settings-field" style="align-self:end;">
              <button type="submit" class="btn btn-primary">Сохранить PMS API</button>
            </div>
          </div>
        </form>
      </div>


      <div class="settings-card">
        <form method="post" action="/admin/settings/pms-check">
          <h3 style="margin:0 0 12px;">Проверка PMS авторизации</h3>

          <div class="settings-grid" style="grid-template-columns:minmax(220px,1fr) 130px minmax(220px,1fr) 170px;">
            <div class="settings-field">
              <label>Объект</label>
              <select name="hotel" required>
                {"".join(pms_check_options)}
              </select>
            </div>

            <div class="settings-field">
              <label>Комната</label>
              <input type="text"
                   name="room_num"
                   value="{escape(pms_check_room)}"
                   placeholder="101"
                   required>
            </div>

            <div class="settings-field">
              <label>Фамилия</label>
              <input type="text"
                   name="surname"
                   value="{escape(pms_check_surname)}"
                   placeholder="Ivanov"
                   required>
            </div>

            <div class="settings-field" style="align-self:end;">
              <button type="submit" class="btn btn-primary">Проверить</button>
            </div>
          </div>

          {pms_check_result_html}
        </form>
      </div>


      <div class="settings-card">
        <h3 style="margin:0 0 12px;">1C / PMS объекты</h3>

        <div class="settings-table-wrap">
          <table class="sessions-table">
            <thead>
              <tr>
                <th>Имя</th>
                <th>Код</th>
                <th>URL</th>
                <th>Токен</th>
                <th>Включено</th>
                <th>Timeout</th>
                <th>Действия</th>
              </tr>
            </thead>
            <tbody>
              {onec_rows}
            </tbody>
          </table>
        </div>

        <form method="post" action="/admin/settings/onec" style="margin-top:18px;">
          <h3 style="margin:0 0 12px;">Добавить / обновить объект 1C</h3>

          <div class="settings-grid settings-grid-onec">
            <div class="settings-field">
              <label>Имя объекта</label>
              <input type="text" name="name" placeholder="Gorod Mira">
            </div>

            <div class="settings-field">
              <label>Код объекта</label>
              <input type="text" name="code" placeholder="gorod_mira">
            </div>

            <div class="settings-field">
              <label>Base URL</label>
              <input type="text" name="base_url" placeholder="https://example.local/api">
            </div>

            <div class="settings-field">
              <label>Token</label>
              <input type="password" name="token" placeholder="Оставить пустым, чтобы не менять">
            </div>

            <div class="settings-field">
              <label>Timeout, sec</label>
              <input type="number" name="timeout" value="5">
            </div>

            <div class="settings-field">
              <label>Включено</label>
              <select name="enabled">
                <option value="1" selected>Да</option>
                <option value="0">Нет</option>
              </select>
            </div>

            <div class="settings-field" style="align-self:end;">
              <button type="submit" class="btn btn-primary">Сохранить объект 1C</button>
            </div>
          </div>
        </form>
      </div>

      <div class="settings-card">
        <h3 style="margin:0 0 12px;">Opera / FIAS объекты</h3>

        <div class="settings-table-wrap">
          <table class="sessions-table">
            <thead>
              <tr>
                <th>Имя</th>
                <th>Код</th>
                <th>Host</th>
                <th>Port</th>
                <th>SSL</th>
                <th>Property</th>
                <th>Auth Key</th>
                <th>Enabled</th>
                <th>Действия</th>
              </tr>
            </thead>
            <tbody>
              {opera_rows}
            </tbody>
          </table>
        </div>

        <form method="post" action="/admin/settings/opera" style="margin-top:18px;">
          <h3 style="margin:0 0 12px;">Добавить / обновить Opera объект</h3>

          <div class="settings-grid settings-grid-opera">
            <div class="settings-field">
              <label>Имя объекта</label>
              <input type="text" name="name" placeholder="Dusit">
            </div>

            <div class="settings-field">
              <label>Код объекта</label>
              <input type="text" name="code" placeholder="dusit">
            </div>

            <div class="settings-field">
              <label>Host</label>
              <input type="text" name="host" placeholder="192.168.0.1">
            </div>

            <div class="settings-field">
              <label>Port</label>
              <input type="number" name="port" value="5057">
            </div>

            <div class="settings-field">
              <label>Property code</label>
              <input type="text" name="property_code" placeholder="DUSIT">
            </div>

            <div class="settings-field">
              <label>Auth key</label>
              <input type="password" name="auth_key" placeholder="Оставить пустым, чтобы не менять">
            </div>

            <div class="settings-field">
              <label>Connect timeout</label>
              <input type="number" name="connect_timeout" value="10">
            </div>

            <div class="settings-field">
              <label>Reconnect seconds</label>
              <input type="number" name="reconnect_seconds" value="30">
            </div>

            <div class="settings-field">
              <label>SSL</label>
              <select name="use_ssl">
                <option value="0" selected>Нет</option>
                <option value="1">Да</option>
              </select>
            </div>

            <div class="settings-field">
              <label>Включено</label>
              <select name="enabled">
                <option value="1" selected>Да</option>
                <option value="0">Нет</option>
              </select>
            </div>

            <div class="settings-field" style="align-self:end;">
              <button type="submit" class="btn btn-primary">Сохранить Opera объект</button>
            </div>
          </div>
        </form>
      </div>

    </div>
    """


    return body

@app.get("/admin/settings", response_class=HTMLResponse)
def admin_settings_page(request: Request, ok: str = ""):
    guard = role_guard(request, ("superadmin",))
    if guard:
        return guard
    return RedirectResponse(url=f"/admin/system?section=settings&ok={escape(ok)}", status_code=303)


@app.post("/admin/settings/mikrotik")
def admin_settings_mikrotik_save(
    request: Request,
    host: str = Form(""),
    port: int = Form(8728),
    user: str = Form(""),
    password: str = Form(""),
    device_sync_interval: int = Form(300),
):
    guard = role_guard(request, ("superadmin",))
    if guard:
        return guard

    set_setting("mikrotik.host", host.strip())
    set_setting("mikrotik.port", port)
    set_setting("mikrotik.user", user.strip())
    set_setting("mikrotik.device_sync_interval", device_sync_interval)

    if password.strip():
        set_setting("mikrotik.password", password.strip(), is_secret=True)

    return RedirectResponse(url="/admin/system?section=settings&ok=1", status_code=303)


@app.post("/admin/settings/radius-api")
def admin_settings_radius_api(
    request: Request,
    radius_allowed_ips: str = Form(""),
):
    guard = role_guard(request, ("superadmin",))
    if guard:
        return guard

    set_setting("radius.allowed_ips", radius_allowed_ips.strip())

    return RedirectResponse(url="/admin/system?section=settings&ok=radius", status_code=303)


@app.post("/admin/settings/pbx")
def admin_settings_pbx(
    request: Request,
    pbx_enabled: str = Form("0"),
    pbx_allowed_ips: str = Form(""),
):
    guard = role_guard(request, ("superadmin",))
    if guard:
        return guard

    set_setting("pbx.enabled", "1" if pbx_enabled == "1" else "0")
    set_setting("pbx.allowed_ips", pbx_allowed_ips.strip())

    return RedirectResponse(url="/admin/system?section=settings&ok=pbx", status_code=303)


@app.post("/admin/settings/pms-api")
def admin_settings_pms_api(
    request: Request,
    pms_api_enabled: str = Form("0"),
    pms_allowed_ips: str = Form(""),
    pms_api_token: str = Form(""),
):
    guard = role_guard(request, ("superadmin",))
    if guard:
        return guard

    set_setting("pms_api.enabled", "1" if pms_api_enabled == "1" else "0")
    set_setting("pms_api.allowed_ips", pms_allowed_ips.strip())

    if pms_api_token.strip():
        set_setting("pms_api.token", pms_api_token.strip(), is_secret=True)

    return RedirectResponse(url="/admin/system?section=settings&ok=pms_api", status_code=303)


@app.post("/admin/settings/pms-check", response_class=HTMLResponse)
def admin_settings_pms_check(
    request: Request,
    hotel: str = Form(""),
    room_num: str = Form(""),
    surname: str = Form(""),
):
    guard = role_guard(request, ("superadmin",))
    if guard:
        return guard

    username, role = get_current_admin_user(request)

    hotel = hotel.strip()
    room_num = room_num.strip()
    surname = surname.strip()

    result = pms_room_auth_allowed(room_num, surname, hotel=hotel)
    pms_check = {
        "hotel": hotel,
        "room_num": room_num,
        "surname": surname,
        "result": result,
    }

    content = build_system_tabs("settings") + build_settings_body(pms_check=pms_check)
    return admin_page("Система", content, active_tab="system", role=role)


@app.post("/admin/settings/onec")
def admin_settings_onec_save(
    request: Request,
    name: str = Form(""),
    code: str = Form(""),
    base_url: str = Form(""),
    token: str = Form(""),
    enabled: int = Form(1),
    timeout: int = Form(5),
):
    guard = role_guard(request, ("superadmin",))
    if guard:
        return guard

    if name.strip() and code.strip():
        upsert_onec_site(
            name=name,
            code=code,
            base_url=base_url,
            token=token,
            enabled=bool(enabled),
            timeout=timeout,
        )

    return RedirectResponse(url="/admin/system?section=settings&ok=1", status_code=303)


@app.post("/admin/settings/onec/delete")
def admin_settings_onec_delete(
    request: Request,
    site_id: int = Form(...),
):
    guard = role_guard(request, ("superadmin",))
    if guard:
        return guard

    delete_onec_site(site_id)

    return RedirectResponse(url="/admin/system?section=settings&ok=1", status_code=303)


@app.post("/admin/settings/opera")
def admin_settings_opera_save(
    request: Request,
    name: str = Form(""),
    code: str = Form(""),
    host: str = Form(""),
    port: int = Form(5057),
    use_ssl: int = Form(0),
    auth_key: str = Form(""),
    property_code: str = Form(""),
    enabled: int = Form(1),
    connect_timeout: int = Form(10),
    reconnect_seconds: int = Form(30),
):
    guard = role_guard(request, ("superadmin",))
    if guard:
        return guard

    if name.strip() and code.strip() and host.strip():
        upsert_opera_site(
            name=name,
            code=code,
            host=host,
            port=port,
            use_ssl=bool(use_ssl),
            auth_key=auth_key,
            property_code=property_code,
            enabled=bool(enabled),
            connect_timeout=connect_timeout,
            reconnect_seconds=reconnect_seconds,
        )

    return RedirectResponse(url="/admin/system?section=settings&ok=1", status_code=303)


@app.post("/admin/settings/opera/delete")
def admin_settings_opera_delete(
    request: Request,
    site_id: int = Form(...),
):
    guard = role_guard(request, ("superadmin",))
    if guard:
        return guard

    delete_opera_site(site_id)

    return RedirectResponse(url="/admin/system?section=settings&ok=1", status_code=303)


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(error: str = ""):
    error_html = ""

    if error == "bad_credentials":
        error_html = """
        <div class="notice error" style="margin-bottom:14px; text-align:center;">
          Неверный логин или пароль
        </div>
        """

    elif error == "disabled":
        error_html = """
        <div class="notice error" style="margin-bottom:14px; text-align:center;">
          Пользователь отключен
        </div>
        """

    return HTMLResponse(f"""
    <!doctype html>
    <html lang="ru">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Miracleon Captive Portal — вход</title>
      <link rel="stylesheet" href="/static/admin.css">
    </head>
    <body class="login-page">
      <div class="login-wrap">
        <div class="login-card">
          <div class="login-brand">MIRACLEON WI-FI</div>
          <h1>Вход в панель</h1>
          <div class="login-subtitle">Управление гостевым Wi-Fi, ваучерами и сессиями</div>

          {error_html}

          <form method="post" action="/admin/login">
            <label>Логин</label>
            <input type="text" name="username" required autocomplete="username">

            <label>Пароль</label>
            <input type="password" name="password" required autocomplete="current-password">

            <button class="btn primary login-btn" type="submit">Войти</button>
          </form>
        </div>
      </div>
    </body>
    </html>
    """)


@app.post("/admin/login")
def admin_login(username: str = Form(...), password: str = Form(...)):
    row = fetch_one("""
        SELECT username, password_hash, role, is_active
        FROM admin_users
        WHERE username = ?
    """, (username,))

    if not row:
        return RedirectResponse(url="/admin/login?error=bad_credentials", status_code=303)

    if int(row["is_active"]) != 1:
        return RedirectResponse(url="/admin/login?error=disabled", status_code=303)

    if not verify_admin_password(password, row["password_hash"]):
        return RedirectResponse(url="/admin/login?error=bad_credentials", status_code=303)

    role = row["role"]

    redirect_url = "/admin"

    if role == "reception":
        redirect_url = "/admin/vouchers"

    resp = RedirectResponse(url=redirect_url, status_code=303)

    resp.set_cookie(
        key=ADMIN_COOKIE,
        value=make_admin_token(username, role),
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=ADMIN_SESSION_TTL_SECONDS
    )

    return resp


@app.get("/admin/logout")
def admin_logout():
    resp = RedirectResponse(url="/admin/login", status_code=303)
    resp.delete_cookie(ADMIN_COOKIE)
    return resp


@app.get("/admin/export/full")
def admin_export_full(request: Request):
    guard = role_guard(request, ("superadmin",))
    if guard:
        return guard

    username, role = get_current_admin_user(request)

    data = build_export_zip()
    return StreamingResponse(
        data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="hotspot_export_full.zip"'}
    )


@app.get("/admin/export/period")
def admin_export_period(request: Request, date_from: str | None = None, date_to: str | None = None):
    guard = role_guard(request, ("superadmin",))
    if guard:
        return guard

    username, role = get_current_admin_user(request)

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
    init_settings_table()
    ensure_security_default_settings()
    init_onec_sites_table()
    init_opera_sites_table()
    ensure_admin_users_table()
    ensure_guests_auth_columns()
    bootstrap_admin_users()

    ensure_guests_last_auth_at()
    ensure_guest_sessions_device_name()
    ensure_guest_sessions_room_auth_columns()
    ensure_terminate_cause_columns()
    ensure_voucher_tables()


@app.post("/pbx-call")
def pbx_call(request: Request, payload: CallIn):
    client_ip = request.client.host if request.client else ""
    pbx_api_guard(request)

    log_phone_event("PHONE_CALL_RECEIVED", payload.phone, source_ip=client_ip)

    try:
        phone = normalize_phone(payload.phone)

        log_phone_event("PHONE_CALL_NORMALIZED", phone, source_ip=client_ip)

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
        """, (phone, phone, client_ip, now_iso(), "no_pending"))
        conn.commit()
        conn.close()
        audit("call_no_pending", phone=phone, details="PBX call without pending auth")

        log_phone_event("PHONE_CALL_NO_PENDING", phone, source_ip=client_ip)
        
        raise HTTPException(status_code=404, detail="pending not found")

    if now() > datetime.fromisoformat(row["expires_at"]):
        conn.execute("UPDATE pending_auth SET status='expired' WHERE id=?", (row["id"],))
        conn.execute("""
            INSERT INTO call_events (phone, callerid_raw, source_ip, created_at, result)
            VALUES (?, ?, ?, ?, ?)
        """, (phone, phone, client_ip, now_iso(), "expired_pending"))
        conn.commit()
        conn.close()
        audit("call_expired_pending", phone=phone, mac=row["mac"], ip=row["ip"], nas_id=row["nas_id"], hotel=row["hotel"], ssid=row["ssid"], vlan_id=row["vlan_id"], details="PBX call matched expired pending")

        log_phone_event(
            "PHONE_CALL_PENDING_EXPIRED",
            phone,
            source_ip=client_ip,
            mac=row["mac"],
            ip=row["ip"],
            hotel=row["hotel"],
            ssid=row["ssid"],
            vlan_id=row["vlan_id"],
        )

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
    """, (phone, phone, client_ip, now_iso(), "matched_pending"))
    conn.commit()
    conn.close()

    audit("call_verified", phone=phone, mac=row["mac"], ip=row["ip"], nas_id=row["nas_id"], hotel=row["hotel"], ssid=row["ssid"], vlan_id=row["vlan_id"], details=f"Phone verified by PBX call, guest_id={guest['id']}")

    log_phone_event(
        "PHONE_CALL_VERIFIED",
        phone,
        source_ip=client_ip,
        mac=row["mac"],
        ip=row["ip"],
        hotel=row["hotel"],
        ssid=row["ssid"],
        vlan_id=row["vlan_id"],
        guest_id=guest["id"],
    )

    return {
        "status": "verified",
        "phone": phone,
        "guest_id": guest["id"]
    }


@app.post("/radius-check")
def radius_check(request: Request, payload: RadiusCheckIn):
    radius_api_guard(request)

    mac = normalize_mac(payload.mac)
    netinfo = resolve_network_info(payload.ip)
    hotel = netinfo["hotel_name"]
    ssid = netinfo["ssid_name"]
    vlan_id = netinfo["vlan_id"]


    # Voucher-auth flow
    raw = (payload.username or "").strip()

    if raw.lower().startswith("voucher:"):
        voucher_code = raw.split(":", 1)[1].strip()

        reply, reason = verify_voucher_radius(
            code=voucher_code,
            mac=mac,
            ip=payload.ip,
            nas_id=payload.nas_id,
            hotel=hotel,
            ssid=ssid,
            vlan_id=vlan_id,
        )

        if reply:
            return reply

        audit(
            "radius_reject_voucher",
            phone=raw,
            mac=mac,
            ip=payload.ip,
            nas_id=payload.nas_id,
            hotel=hotel,
            ssid=ssid,
            vlan_id=vlan_id,
            details=reason
        )
        raise HTTPException(status_code=403, detail=reason)


    # MAC-auth flow.
    # MikroTik may send username as MAC before showing login page.
    raw_as_mac = normalize_mac(raw)

    if raw_as_mac == mac:
        known = get_recent_authorized_session_by_mac(mac)

        if known:
            phone_identity = known["guest_phone"]

            session = get_open_session(phone_identity, mac)
            if session:
                update_session(session["id"], payload.ip, payload.nas_id, hotel, ssid, vlan_id)
            else:
                start_session(
                    known["guest_id"],
                    phone_identity,
                    mac,
                    payload.ip,
                    payload.nas_id,
                    hotel,
                    ssid,
                    vlan_id,
                )

            touch_guest_auth(phone_identity)

            audit(
                "radius_accept_mac_auth",
                phone=phone_identity,
                mac=mac,
                ip=payload.ip,
                nas_id=payload.nas_id,
                hotel=hotel,
                ssid=ssid,
                vlan_id=vlan_id,
                details="Known MAC accepted without portal"
            )
            return accept_reply()

        audit(
            "radius_reject_mac_auth_unknown",
            phone=None,
            mac=mac,
            ip=payload.ip,
            nas_id=payload.nas_id,
            hotel=hotel,
            ssid=ssid,
            vlan_id=vlan_id,
            details="MAC not found or auth expired"
        )
        raise HTTPException(status_code=403, detail="mac_auth_unknown")


    # Dusit room-auth through current RADIUS flow
    # Room-auth only when username has room|surname format.
    # Plain phone numbers must continue to the normal call-auth flow below.
    

    if "|" in raw:

        room_num, surname = [x.strip() for x in raw.split("|", 1)]

        if not room_num or not surname:
            audit(
                "radius_reject_room_missing_room_or_surname",
                phone=raw,
                mac=mac,
                ip=payload.ip,
                nas_id=payload.nas_id,
                hotel=hotel,
                ssid=ssid,
                vlan_id=vlan_id,
                details="room or surname empty"
            )
            raise HTTPException(status_code=403, detail="room_and_surname_required")

        room_identity = make_room_identity(room_num, surname)

        existing_guest = get_active_guest(room_identity)
        if existing_guest:
            touch_guest_auth(room_identity)

            session = get_open_session(room_identity, mac)
            if session:
                update_session(session["id"], payload.ip, payload.nas_id, hotel, ssid, vlan_id)
            else:
                start_session(
                    existing_guest["id"],
                    room_identity,
                    mac,
                    payload.ip,
                    payload.nas_id,
                    hotel,
                    ssid,
                    vlan_id,
                )

                conn = db()
                conn.execute("""
                    UPDATE guest_sessions
                    SET auth_method = 'room',
                        room_num = ?,
                        surname = ?
                    WHERE id = (
                        SELECT id
                        FROM guest_sessions
                        WHERE phone = ? AND mac = ? AND status = 'active'
                        ORDER BY id DESC
                        LIMIT 1
                    )
                """, (room_num, surname, room_identity, mac))
                conn.commit()
                conn.close()

            audit(
                "radius_accept_existing_room_identity",
                phone=room_identity,
                mac=mac,
                ip=payload.ip,
                nas_id=payload.nas_id,
                hotel=hotel,
                ssid=ssid,
                vlan_id=vlan_id,
                details=f"Existing room identity accepted without PMS: room={room_num} surname={surname}"
            )

            logger.info("ROOM_AUTH_ACCEPT_EXISTING %s", {
                "room": room_num,
                "surname": surname,
                "ip": payload.ip,
                "hotel": hotel,
                "source": "existing_guest",
            })

            return accept_reply()

        pms_result = pms_room_auth_allowed(
            room_num,
            surname,
            hotel=hotel,
            vlan_id=vlan_id,
        )

        if pms_result.get("ok"):
            guest = get_or_create_room_guest(room_num, surname, hotel)

            session = get_open_session(room_identity, mac)
            if session:
                update_session(session["id"], payload.ip, payload.nas_id, hotel, ssid, vlan_id)
            else:
                start_session(guest["id"], room_identity, mac, payload.ip, payload.nas_id, hotel, ssid, vlan_id)

                conn = db()
                conn.execute("""
                    UPDATE guest_sessions
                    SET auth_method = 'room',
                        room_num = ?,
                        surname = ?
                    WHERE id = (
                        SELECT id
                        FROM guest_sessions
                        WHERE phone = ? AND mac = ? AND status = 'active'
                        ORDER BY id DESC
                        LIMIT 1
                    )
                """, (room_num, surname, room_identity, mac))
                conn.commit()
                conn.close()

            audit(
                "radius_accept_room_auth",
                phone=room_identity,
                mac=mac,
                ip=payload.ip,
                nas_id=payload.nas_id,
                hotel=hotel,
                ssid=ssid,
                vlan_id=vlan_id,
                details=f"room={room_num} surname={surname}"
            )
            logger.info("ROOM_AUTH_ACCEPT %s", {
                "room": room_num,
                "surname": surname,
                "ip": payload.ip,
                "hotel": hotel,
                "source": pms_result.get("source"),
                "reservation": pms_result.get("reservation_number", ""),
            })
            return accept_reply()

        audit(
            "radius_reject_room_auth",
            phone=room_identity,
            mac=mac,
            ip=payload.ip,
            nas_id=payload.nas_id,
            hotel=hotel,
            ssid=ssid,
            vlan_id=vlan_id,
            details=f"room={room_num} surname={surname}"
        )
        
        logger.info("ROOM_AUTH_REJECT %s", {
            "room": room_num,
            "surname": surname,
            "ip": payload.ip,
            "hotel": hotel,
            "source": pms_result.get("source"),
            "error": pms_result.get("error"),
        })
        raise HTTPException(status_code=403, detail="room_auth_not_found")

    try:
        phone = normalize_phone(payload.username)

        log_phone_event(
            "PHONE_RADIUS_CHECK",
            phone,
            mac=mac,
            ip=payload.ip,
            hotel=hotel,
            ssid=ssid,
            vlan_id=vlan_id,
        )

    except ValueError:

        log_phone_event(
            "PHONE_RADIUS_INVALID",
            payload.username,
            mac=mac,
            ip=payload.ip,
            hotel=hotel,
            ssid=ssid,
            vlan_id=vlan_id,
        )

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

            log_phone_event(
                "PHONE_RADIUS_ACCEPT_EXISTING_SESSION",
                phone,
                mac=mac,
                ip=payload.ip,
                hotel=hotel,
                ssid=ssid,
                vlan_id=vlan_id,
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

            log_phone_event(
                "PHONE_RADIUS_REJECT_DEVICE_LIMIT",
                phone,
                mac=mac,
                ip=payload.ip,
                hotel=hotel,
                ssid=ssid,
                vlan_id=vlan_id,
                limit=DEVICE_LIMIT,
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

        log_phone_event(
            "PHONE_RADIUS_ACCEPT_NEW_SESSION",
            phone,
            mac=mac,
            ip=payload.ip,
            hotel=hotel,
            ssid=ssid,
            vlan_id=vlan_id,
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

                log_phone_event(
                    "PHONE_RADIUS_REJECT_PENDING_EXPIRED",
                    phone,
                    mac=mac,
                    ip=payload.ip,
                    hotel=hotel,
                    ssid=ssid,
                    vlan_id=vlan_id,
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

        log_phone_event(
            "PHONE_RADIUS_REJECT_WAITING_CALL",
            phone,
            mac=mac,
            ip=payload.ip,
            hotel=hotel,
            ssid=ssid,
            vlan_id=vlan_id,
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

    log_phone_event(
        "PHONE_RADIUS_PENDING_CREATED",
        phone,
        mac=mac,
        ip=payload.ip,
        hotel=hotel,
        ssid=ssid,
        vlan_id=vlan_id,
        expires_at=expires.isoformat(),
    )
    
    raise HTTPException(status_code=403, detail="pending_created")


@app.post("/auth/dusit/authorize")
def auth_dusit_authorize(request: Request, payload: dict = Body(...)):
    pms_api_guard(request)

    room_num = (payload.get("room_num") or "").strip()
    surname = (payload.get("surname") or "").strip()
    mac = (payload.get("mac") or "").strip()
    ip = (payload.get("ip") or "").strip()
    nas_id = (payload.get("nas_id") or "").strip()

    if not room_num or not surname or not mac or not ip:
        raise HTTPException(status_code=400, detail="room_num_surname_mac_ip_required")

    pms_result = pms_room_auth_allowed(room_num, surname, hotel="Dusit")
    if not pms_result.get("ok"):
        return {
            "ok": False,
            "status": "not_found",
            "error": pms_result.get("error") or "",
            "source": pms_result.get("source"),
        }

    save_verified_room_auth(
        room_num=room_num,
        surname=surname,
        surname_norm=normalize_user_surname(surname),
        mac=mac,
        ip=ip,
        nas_id=nas_id,
        hotel="Dusit"
    )
    return {"ok": True, "status": "ok"}


@app.get("/auth-status")
def auth_status(request: Request, phone: str = Query(...)):
    pms_api_guard(request)

    try:
        phone = normalize_phone(phone)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid phone")

    guest = get_active_guest(phone)
    if guest:

        log_phone_event("PHONE_AUTH_STATUS_VERIFIED", phone)
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

            log_phone_event("PHONE_AUTH_STATUS_EXPIRED", phone)
            return {"status": "expired"}
        conn.close()
        return {"status": "pending"}

    conn.close()
    log_phone_event("PHONE_AUTH_STATUS_NOT_FOUND", phone)
    return {"status": "not_found"}


@app.post("/radius-accounting")
def radius_accounting(request: Request, payload: RadiusAccountingIn):
    radius_api_guard(request)

    save_radius_accounting(payload)
    return {"status": "ok"}


@app.get("/admin", response_class=HTMLResponse)
def admin_index(request: Request, denied: str = ""):
    guard = role_guard(request, ("admin", "superadmin", "it"))
    if guard:
        return guard

    username, role = get_current_admin_user(request)

    denied_html = ""
    if denied:
        denied_html = """
        <div class="notice error" style="margin-bottom:14px;">
          Доступ запрещён. У вашей роли нет прав для открытия этого раздела.
        </div>
        """

    disk = get_disk_usage("/")
    disk_percent = disk["percent"]
    memory = get_memory_usage()
    cpu = get_cpu_load()
    uptime = get_uptime()

    if disk_percent >= 85:
        disk_class = "error"
    elif disk_percent >= 70:
        disk_class = "pending"
    else:
        disk_class = "active"

    guests_cnt = fetch_all("SELECT COUNT(*) AS cnt FROM guests")[0]["cnt"]
    sessions_cnt = fetch_all("SELECT COUNT(*) AS cnt FROM guest_sessions WHERE status='active' AND ended_at IS NULL AND datetime(last_seen_at) >= datetime('now', '-15 minutes')")[0]["cnt"]
    pending_cnt = fetch_all("SELECT COUNT(*) AS cnt FROM pending_auth WHERE status='pending'")[0]["cnt"]
    calls_cnt = fetch_all("SELECT COUNT(*) AS cnt FROM call_events WHERE date(created_at)=date('now')")[0]["cnt"]

    body = denied_html + f"""
    <div class="stats">
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
      <div class="stat">
        <div class="stat-label">По ваучеру сегодня</div>
        <div class="stat-value" id="stat-auth-voucher">0</div>
      </div>
      <div class="stat">
        <div class="stat-label">По комнате сегодня</div>
        <div class="stat-value" id="stat-auth-room">0</div>
      </div>
    </div>

    <div id="hotspot-sites" class="hotspot-sites"></div>

    <div class="card dashboard-chart-card">
      <h2 style="margin:0 0 12px; font-size:24px;">Активность подключений</h2>

      <div style="display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:14px; flex-wrap:wrap;">
        <div style="font-size:14px; color:#6b7280;">Период отображения</div>
        <select id="chartPeriod">
          <option value="1h">Час</option>
          <option value="1d" selected>День</option>
          <option value="1mo">Месяц</option>
          <option value="1y">Год</option>
        </select>
      </div>

      <div class="chart-controls">
        <label class="chart-toggle">
          <input type="checkbox" id="toggle-sessions" checked>
          <span class="toggle-dot"></span>
          <span>Активные сессии</span>
        </label>

        <label class="chart-toggle">
          <input type="checkbox" id="toggle-auth-total">
          <span class="toggle-dot"></span>
          <span>Все авторизации</span>
        </label>

        <label class="chart-toggle">
          <input type="checkbox" id="toggle-auth-phone" checked>
          <span class="toggle-dot"></span>
          <span>По телефону</span>
        </label>

        <label class="chart-toggle">
          <input type="checkbox" id="toggle-auth-voucher" checked>
          <span class="toggle-dot"></span>
          <span>По ваучеру</span>
        </label>

        <label class="chart-toggle">
          <input type="checkbox" id="toggle-auth-room" checked>
          <span class="toggle-dot"></span>
          <span>По комнате</span>
        </label>
      </div>

      <div class="chart-box">
        <canvas id="activityChart"></canvas>
      </div>
    </div>

    <div class="system-stats system-stats-compact">
      <div class="stat">
        <div class="stat-label">Гостей в базе</div>
        <div class="stat-value" id="stat-guests">{guests_cnt}</div>
      </div>

      <div class="stat disk-stat">
        <div class="stat-label">Диск</div>
        <div class="stat-row">
          <div class="stat-value">{disk_percent}%</div>
          <div class="muted">{disk["used_h"]} / {disk["total_h"]}</div>
        </div>
        <div class="disk-bar">
          <div class="badge {disk_class} disk-fill" style="width:{disk_percent}%;"></div>
        </div>
      </div>

      <div class="stat">
        <div class="stat-label">CPU</div>
        <div class="stat-row">
          <div class="stat-value">{cpu["percent"]}%</div>
          <div class="muted">load {cpu["load1"]} / {cpu["cores"]} cores</div>
        </div>
      </div>

      <div class="stat">
        <div class="stat-label">RAM</div>
        <div class="stat-row">
          <div class="stat-value">{memory["percent"]}%</div>
          <div class="muted">{memory["used_h"]} / {memory["total_h"]}</div>
        </div>
      </div>

      <div class="stat">
        <div class="stat-label">Uptime</div>
        <div class="stat-row">
          <div class="stat-value">{uptime["text"]}</div>
          <div class="muted">сервер работает</div>
        </div>
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
          document.getElementById('stat-auth-voucher').textContent = data.stats.auth_voucher_today;
          document.getElementById('stat-auth-room').textContent = data.stats.auth_room_today;

          const sitesBox = document.getElementById('hotspot-sites');

          if (sitesBox && data.hotspot_sites) {{
            sitesBox.innerHTML = data.hotspot_sites.map((item) => `
              <div class="hotspot-site-card">
                <div class="hotspot-site-name">${{item.name}}</div>
                <div class="hotspot-site-value">${{item.active}}</div>
                <div class="hotspot-site-label">активных устройств</div>
              </div>
            `).join('');
          }}

          const labels = data.chart.labels;
          
          if (!activityChart) {{
            const ctx = document.getElementById('activityChart').getContext('2d');
            activityChart = new Chart(ctx, {{
              type: 'line',
              data: {{
                labels: labels,
                datasets: [
                  {{
                    label: 'Активные сессии',
                    data: data.chart.active_sessions,
                    yAxisID: 'y',
                    tension: 0.35,
                    fill: true,
                    borderWidth: 3,
                    pointRadius: 0,
                    hidden: !toggleSessions.checked
                  }},

                  {{
                    label: 'Все авторизации',
                    data: data.chart.auth_total,
                    yAxisID: 'y1',
                    tension: 0.35,
                    fill: false,
                    borderWidth: 2,
                    pointRadius: 0,
                    hidden: !toggleTotal.checked
                  }},

                  {{
                    label: 'По телефону',
                    data: data.chart.auth_call,
                    yAxisID: 'y1',
                    tension: 0.35,
                    fill: false,
                    borderWidth: 2,
                    pointRadius: 0,
                    hidden: !togglePhone.checked
                  }},

                  {{
                    label: 'По ваучеру',
                    data: data.chart.auth_voucher,
                    yAxisID: 'y1',
                    tension: 0.35,
                    fill: false,
                    borderWidth: 2,
                    pointRadius: 0,
                    hidden: !toggleVoucher.checked
                  }},

                  {{
                    label: 'По комнате',
                    data: data.chart.auth_room,
                    yAxisID: 'y1',
                    tension: 0.35,
                    fill: false,
                    borderWidth: 2,
                    pointRadius: 0,
                    hidden: !toggleRoom.checked
                  }}
                ]
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
                    ticks: {{
                      color: '#111827',
                      font: {{
                        size: 12,
                        weight: '700'
                      }}
                    }},
                    grid: {{
                      color: 'rgba(17,24,39,.12)'
                    }}
                  }},
                  y: {{
                    beginAtZero: true,
                    position: 'left',
  
                    ticks: {{
                        color: '#111827',
                        precision: 0,
                        font: {{
                          size: 12,
                          weight: '700'
                        }}
                    }},

                    grid: {{
                        color: 'rgba(17,24,39,.14)'
                    }}
                    }},

                  y1: {{
                    display: true,
                    beginAtZero: true,
                    position: 'right',

                    ticks: {{
                      color: '#111827',
                      precision: 0,
                      font: {{
                        size: 12,
                        weight: '700'
                      }}
                    }},

                    grid: {{
                      drawOnChartArea: false
                    }}
                  }}
                }}
              }}
            }});
          updateChartVisibility();
          }} else {{
            activityChart.data.labels = labels;
            activityChart.data.datasets[0].data = data.chart.active_sessions;
            activityChart.data.datasets[1].data = data.chart.auth_total;
            activityChart.data.datasets[2].data = data.chart.auth_call;
            activityChart.data.datasets[3].data = data.chart.auth_voucher;
            activityChart.data.datasets[4].data = data.chart.auth_room;            
            activityChart.update();
          }}
        }} catch (e) {{
          console.error('dashboard update failed', e);
        }}
      }}

      const toggleSessions = document.getElementById('toggle-sessions');
      const toggleTotal = document.getElementById('toggle-auth-total');
      const togglePhone = document.getElementById('toggle-auth-phone');
      const toggleVoucher = document.getElementById('toggle-auth-voucher');
      const toggleRoom = document.getElementById('toggle-auth-room');
      const STORAGE_KEY = 'dashboard_chart_toggles';

      function saveToggleState() {{
        localStorage.setItem(STORAGE_KEY, JSON.stringify({{
          sessions: toggleSessions.checked,
          total: toggleTotal.checked,
          phone: togglePhone.checked,
          voucher: toggleVoucher.checked,
          room: toggleRoom.checked
        }}));
      }}

      function loadToggleState() {{
        try {{
          const saved = JSON.parse(localStorage.getItem(STORAGE_KEY));

          if (!saved) return;

          toggleSessions.checked = !!saved.sessions;
          toggleTotal.checked = !!saved.total;
          togglePhone.checked = !!saved.phone;
          toggleVoucher.checked = !!saved.voucher;
          toggleRoom.checked = !!saved.room;

        }} catch (e) {{
          console.error('toggle restore failed', e);
        }}
      }}

      function updateChartVisibility() {{
        if (!activityChart) return;
        activityChart.data.datasets[0].hidden = !toggleSessions.checked;
        activityChart.data.datasets[1].hidden = !toggleTotal.checked;
        activityChart.data.datasets[2].hidden = !togglePhone.checked;
        activityChart.data.datasets[3].hidden = !toggleVoucher.checked;
        activityChart.data.datasets[4].hidden = !toggleRoom.checked;
        
        const showAuthAxis =
          toggleTotal.checked ||
          togglePhone.checked ||
          toggleVoucher.checked ||
          toggleRoom.checked;

        activityChart.options.scales.y1.display = showAuthAxis;

        activityChart.update();
      }}

      function syncAuthToggles(changed) {{
        if (changed === toggleTotal && toggleTotal.checked) {{
          togglePhone.checked = false;
          toggleVoucher.checked = false;
          toggleRoom.checked = false;
        }}

        if (
          (changed === togglePhone || changed === toggleVoucher || changed === toggleRoom) &&
          changed.checked
        ) {{
          toggleTotal.checked = false;
        }}

        const anyEnabled =
            toggleSessions.checked ||
            toggleTotal.checked ||
            togglePhone.checked ||
            toggleVoucher.checked ||
            toggleRoom.checked;

          if (!anyEnabled) {{
            toggleSessions.checked = true;
          }}

            updateChartVisibility();
            saveToggleState();

        updateChartVisibility();
      }}

      [toggleSessions, toggleTotal, togglePhone, toggleVoucher, toggleRoom].forEach((el) => {{
        if (el) {{
          el.addEventListener('change', () => syncAuthToggles(el));
        }}
      }});

      document.getElementById('chartPeriod').addEventListener('change', loadDashboardData);

      
      loadToggleState();
      loadDashboardData();
      setInterval(loadDashboardData, 30000);
    </script>
    """
    return admin_page("Панель управления", body, active_tab="home", role=role)


@app.get("/admin/guests", response_class=HTMLResponse)
def admin_guests(request: Request):

    guard = role_guard(request, ("admin", "superadmin", "it"))
    if guard:
        return guard

    username, role = get_current_admin_user(request)

    rows = fetch_all("""
        SELECT id, phone, auth_type, room_number, guest_name, first_verified_at, first_hotel, auth_method, status, created_at, updated_at
        FROM guests
        ORDER BY created_at DESC
        LIMIT 300
    """)

    trs = ""

    for row in rows:
        guest_id = row["id"]
        if row["auth_type"] == "room":
            guest_label = f'Комната {row["room_number"] or ""} — {row["guest_name"] or ""}'
        else:
            guest_label = str(row["phone"] or "")

        guest_label = escape(guest_label)

        trs += f"""
        <tr>
          <td><a href="/admin/client?guest_id={guest_id}">{guest_id}</a></td>
          <td><a href="/admin/client?guest_id={guest_id}">{guest_label}</a></td>
          <td>{escape(format_dt(row["first_verified_at"]))}</td>
          <td>{escape(str(row["first_hotel"] or ""))}</td>
          <td>{escape(str(row["auth_method"] or ""))}</td>
          <td>{escape(str(row["status"] or ""))}</td>
          <td>{escape(format_dt(row["created_at"]))}</td>
          <td>{escape(format_dt(row["updated_at"]))}</td>
        </tr>
        """

    body = f"""
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>Клиент</th>
            <th>Первая проверка</th>
            <th>Первый объект</th>
            <th>Метод</th>
            <th>Статус</th>
            <th>Создан</th>
            <th>Обновлён</th>
          </tr>
        </thead>
        <tbody>
          {trs}
        </tbody>
      </table>
    </div>
    """

    return admin_page("Гости", body, active_tab="guests", role=role)


@app.get("/admin/sessions", response_class=HTMLResponse)
def admin_sessions(
    request: Request,
    q: str = "",
    status: str = "all",
    hotel: str = "",
    ssid: str = "",
    vlan_id: str = "",
    terminate_cause: str = "",
    date_from: str = "",
    date_to: str = "",
):
    guard = role_guard(request, ("admin", "superadmin", "it"))
    if guard:
        return guard

    username, role = get_current_admin_user(request)

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

    if ssid.strip():
        where.append("ssid = ?")
        params.append(ssid.strip())

    if vlan_id.strip():
        where.append("vlan_id = ?")
        params.append(vlan_id.strip())

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
        LIMIT 20
        """,
        tuple(params)
    )


    linked_rows = []
    for row in rows:
        row_dict = dict(row)

        if row["auth_method"] == "room":
            client_label = f'Комната {row["room_num"] or ""} — {row["surname"] or ""}'
        else:
            client_label = str(row["phone"] or "")

        row_dict["phone"] = client_label
        row_dict["mac"] = str(row["mac"] or "")

        raw_cause = row["terminate_cause"] or "unknown"
        row_dict["terminate_cause"] = TERMINATE_CAUSE_LABELS.get(raw_cause, raw_cause)

        linked_rows.append(row_dict)


    hotel_rows = fetch_all("""
        SELECT DISTINCT hotel
        FROM guest_sessions
        WHERE hotel IS NOT NULL AND hotel != ''
        ORDER BY hotel
    """)
    hotels = [r["hotel"] for r in hotel_rows]

    ssid_rows = fetch_all("""
        SELECT DISTINCT ssid
        FROM guest_sessions
        WHERE ssid IS NOT NULL AND ssid != ''
        ORDER BY ssid
    """)
    ssids = [r["ssid"] for r in ssid_rows]

    vlan_rows = fetch_all("""
        SELECT DISTINCT vlan_id
        FROM guest_sessions
        WHERE vlan_id IS NOT NULL
          AND TRIM(CAST(vlan_id AS TEXT)) != ''
          AND CAST(vlan_id AS TEXT) GLOB '[0-9]*'
        ORDER BY CAST(vlan_id AS INTEGER), CAST(vlan_id AS TEXT)
    """)
    vlans = [str(r["vlan_id"]) for r in vlan_rows]

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

    ssid_options = ['<option value="">Все Wi-Fi сети</option>']
    for s in ssids:
        selected = " selected" if s == ssid else ""
        ssid_options.append(f'<option value="{escape(s)}"{selected}>{escape(s)}</option>')

    vlan_options = ['<option value="">Все VLAN</option>']
    for v in vlans:
        selected = " selected" if v == vlan_id else ""
        vlan_options.append(f'<option value="{escape(v)}"{selected}>{escape(v)}</option>')

    cause_options = ['<option value="">Все причины</option>']
    for c in causes:
        selected = " selected" if c == terminate_cause else ""
        cause_options.append(f'<option value="{escape(c)}"{selected}>{escape(TERMINATE_CAUSE_LABELS.get(c, c))}</option>')

    status_options = []
    for value, label in [("all", "Все"), ("active", "Только активные"), ("closed", "Только закрытые")]:
        selected = " selected" if value == status else ""
        status_options.append(f'<option value="{value}"{selected}>{label}</option>')

    body = f"""
    <div class="system-section">
      <h2>Фильтр сессий</h2>

      <form class="export-form sessions-export-form" method="get" action="/admin/sessions">
        <div>
          <label style="display:block; margin-bottom:6px; font-weight:600;">Поиск</label>
          <input type="text" name="q" value="{escape(q)}" placeholder="Номер, MAC, IP, session ID">
        </div>

        <div>
          <label style="display:block; margin-bottom:6px; font-weight:600;">Статус</label>
          <select name="status">{''.join(status_options)}</select>
        </div>

        <div>
          <label style="display:block; margin-bottom:6px; font-weight:600;">Объект</label>
          <select name="hotel">{''.join(hotel_options)}</select>
        </div>

        <div>
          <label style="display:block; margin-bottom:6px; font-weight:600;">Wi-Fi сеть</label>
          <select name="ssid">{''.join(ssid_options)}</select>
        </div>

        <div>
          <label style="display:block; margin-bottom:6px; font-weight:600;">VLAN</label>
          <select name="vlan_id">{''.join(vlan_options)}</select>
        </div>

        <div>
          <label style="display:block; margin-bottom:6px; font-weight:600;">Причина завершения</label>
          <select name="terminate_cause">{''.join(cause_options)}</select>
        </div>

        <div>
          <label style="display:block; margin-bottom:6px; font-weight:600;">Дата с</label>
          <input type="date" name="date_from" value="{escape(date_from)}">
        </div>

        <div>
          <label style="display:block; margin-bottom:6px; font-weight:600;">Дата по</label>
          <input type="date" name="date_to" value="{escape(date_to)}">
        </div>

        <div class="sessions-form-actions">
          <button class="btn primary" type="submit">Применить</button>
          <a class="btn" href="/admin/sessions">Сбросить</a>
        </div>
      </form>
    </div>

    <div class="muted" style="margin-bottom:14px;">
      Показано сессий: {len(rows)}
    </div>
    """
    
    table_html = html_table(
        linked_rows,
        [
            "guest_id",
            "phone",
            "auth_method",
            "room_num",
            "surname",
            "mac",
            "ip",
            "device_name",
            "started_at",
            "last_seen_at",
            "ended_at",
            "status",
            "terminate_cause",
            "terminate_cause_raw",
            "acct_session_time",
            "hotel",
            "ssid",
            "vlan_id",
            "nas_id",
            "acct_session_id",
        ]
    )

    body += f"""
    <div class="system-section" style="margin-top:14px; padding:0;">
      {table_html}
    </div>
    """



    return admin_page("Сессии", body, active_tab="sessions", role=role)

@app.get("/admin/pending", response_class=HTMLResponse)
def admin_pending(request: Request):

    guard = role_guard(request, ("admin", "superadmin", "it"))
    if guard:
        return guard

    username, role = get_current_admin_user(request)

    rows = fetch_all("SELECT * FROM pending_auth WHERE status = 'pending' ORDER BY created_at DESC LIMIT 300")
    cols = ["id", "phone", "mac", "ip", "nas_id", "hotel", "ssid", "vlan_id", "created_at", "expires_at", "status"]
    body = html_table(rows, cols)
    return admin_page("Ожидают подтверждения", body, active_tab="pending", role=role)


@app.get("/admin/calls", response_class=HTMLResponse)
def admin_calls(request: Request):

    guard = role_guard(request, ("admin", "superadmin", "it"))
    if guard:
        return guard

    username, role = get_current_admin_user(request)

    rows = fetch_all("SELECT * FROM call_events ORDER BY created_at DESC LIMIT 300")
    cols = ["id", "phone", "callerid_raw", "source_ip", "created_at", "result"]
    body = html_table(rows, cols)
    return admin_page("Звонки", body, active_tab="calls", role=role)


@app.get("/admin/vouchers", response_class=HTMLResponse)
def admin_vouchers(request: Request, error: str = "", ok: str = ""):
    guard = role_guard(request, ("admin", "superadmin", "it", "reception"))
    if guard:
        return guard

    username, role = get_current_admin_user(request)

    msg_html = ""

    errors = {
        "bad_full_name": "Проверьте ФИО: нужно минимум имя и фамилия, без цифр",
        "bad_passport": "Паспорт РФ должен быть в формате 4 цифры серии и 6 цифр номера",
        "bad_foreign_passport": "Загранпаспорт РФ должен содержать 9 цифр",
        "bad_document_type": "Некорректный тип документа",
    }

    if error:
        msg_html = f"""
        <div class="notice error" style="margin-bottom:14px; text-align:center;">
          {errors.get(error, "Ошибка при создании ваучера")}
        </div>
        """

    rows = fetch_all("""
        SELECT
            v.id,
            v.code_enc,
            v.status,
            v.max_devices,
            COUNT(DISTINCT d.mac) AS used_devices,
            v.valid_from,
            v.valid_until,
            v.site,
            v.room_num,
            v.created_by,
            v.created_at
        FROM vouchers v
        LEFT JOIN voucher_devices d ON d.voucher_id = v.id
        GROUP BY v.id
        ORDER BY v.id DESC
        LIMIT 200
    """)

    voucher_rows = []
    for row in rows:
        r = dict(row)
        r["code"] = decrypt_voucher_code(r.get("code_enc"))
        voucher_rows.append(r)

    form = """
    <div class="system-section voucher-create-panel">
      <h2>Создание ваучера</h2>

      <form class="voucher-form" method="post" action="/admin/vouchers/create">

        <div class="voucher-form-row">

          <div>
            <label class="required-label">ФИО гостя</label>
            <input class="required-input"
                   type="text"
                   name="full_name"
                   required
                   placeholder="Иванов Иван Иванович">
          </div>

          <div>
            <label class="required-label">Тип документа</label>
            <select class="required-input"
                    name="document_type"
                    required>
              <option value="rf_passport">Паспорт РФ</option>
              <option value="foreign_passport">Загранпаспорт</option>
            </select>
          </div>

          <div>
            <label class="required-label">Серия и номер</label>
            <input class="required-input"
                   type="text"
                   name="passport"
                   required
                   placeholder="1234 567890">
          </div>

          <div>
            <label>Телефон</label>
            <input type="text"
                   name="phone"
                   placeholder="+7...">
          </div>

          <div>
            <label>Дата рождения</label>
            <input type="date"
                   name="birth_date">
          </div>

          <div>
            <label>Объект</label>
            <input type="text"
                   name="site"
                   placeholder="Dusit">
          </div>

          <div>
            <label>Комната</label>
            <input type="text"
                   name="room_num"
                   placeholder="751">
          </div>

          <div>
            <label>Устр.</label>
            <input type="number"
                   name="max_devices"
                   value="1"
                   min="1"
                   max="3"
                   required>
          </div>

          <div>
            <label>Дней</label>
            <input type="number"
                   name="valid_days"
                   value="1"
                   min="1"
                   max="14"
                   required>
          </div>

        </div>

        <div class="voucher-submit-row">
          <button class="btn primary voucher-submit-btn"
                  type="submit">
            Создать ваучер
          </button>
        </div>

      </form>
    </div>
    """

    body = msg_html + form

    body += "<h2 style='margin:18px 0 12px; font-size:22px;'>Последние ваучеры</h2>"
    
    table_html = """
    <div style="overflow-x:auto;">
      <table class="table">
        <thead>
          <tr>
            <th>ID</th>
            <th>Код</th>
            <th>Статус</th>
            <th>Устройств</th>
            <th>Использовано</th>
            <th>Действует с</th>
            <th>Действует до</th>
            <th>Объект выдачи</th>
            <th>Комната</th>
            <th>Кем создан</th>
            <th>Создан</th>
            <th>Действия</th>
          </tr>
        </thead>
        <tbody>
    """

    def status_badge(status: str) -> str:
        status = (status or "").strip()

        if status == "active":
            return '<span class="badge active">Активен</span>'

        if status == "revoked":
            return '<span class="badge blocked">Отключен</span>'

        if status == "expired":
            return '<span class="badge expired">Истекла</span>'

        return f'<span class="badge">{escape(status)}</span>'

    for r in voucher_rows:
        vid = int(r["id"])
        status = str(r.get("status") or "")
        disabled = "disabled" if status != "active" else ""
        button_text = "Отключить" if status == "active" else "Отключен"
        
        revoke_cell = ""

        if role == "admin":
            revoke_cell = f"""
              <form method="post" action="/admin/vouchers/revoke" style="display:inline-flex; margin:0;" onsubmit="return confirm('Отключить ваучер? Дальнейший вход по нему будет запрещён.');">
                <input type="hidden" name="voucher_id" value="{vid}">
                <button class="btn" type="submit" {disabled}>{button_text}</button>
              </form>
            """

        table_html += f"""
          <tr>
            <td>
              <a href="/admin/vouchers/{vid}" style="text-decoration:none; font-weight:800;">
                {vid}
              </a>
            </td>
            <td style="font-weight:800; white-space:nowrap;">
              <a href="/admin/vouchers/{vid}" style="text-decoration:none;">
                {escape(str(r.get("code") or "—"))}
              </a>
            </td>
            <td>{status_badge(status)}</td>
            <td>{escape(str(r.get("max_devices") or ""))}</td>
            <td>{escape(str(r.get("used_devices") or "0"))}</td>
            <td>{escape(format_dt(r.get("valid_from")))}</td>
            <td>{escape(format_dt(r.get("valid_until")))}</td>
            <td>{escape(str(r.get("site") or ""))}</td>
            <td>{escape(str(r.get("room_num") or ""))}</td>
            <td>{escape(str(r.get("created_by") or ""))}</td>
            <td>{escape(format_dt(r.get("created_at")))}</td>
            <td>
              {revoke_cell}
            </td>
          </tr>
        """

    table_html += """
        </tbody>
      </table>
    </div>
    """

    body += table_html
    
    return admin_page("Ваучеры", body, active_tab="vouchers", role=role)


@app.post("/admin/vouchers/create", response_class=HTMLResponse)
def admin_vouchers_create(
    request: Request,
    full_name: str = Form(...),
    passport: str = Form(...),
    birth_date: str = Form(""),
    phone: str = Form(""),
    site: str = Form(""),
    room_num: str = Form(""),
    max_devices: int = Form(1),
    valid_days: int = Form(1),
    document_type: str = Form(...),
):
    guard = role_guard(request, ("admin", "superadmin", "it", "reception"))
    if guard:
        return guard

    username, role = get_current_admin_user(request)
    full_name = full_name.strip()
    passport_raw = passport.strip()
    document_type = document_type.strip()

    if not re.match(r"^[А-Яа-яЁёA-Za-z\s-]{5,}$", full_name) or len(full_name.split()) < 2:
        return RedirectResponse(url="/admin/vouchers?error=bad_full_name", status_code=303)

    passport_digits = re.sub(r"\D", "", passport_raw)

    if document_type == "rf_passport":
        if not re.match(r"^\d{10}$", passport_digits):
            return RedirectResponse(url="/admin/vouchers?error=bad_passport", status_code=303)

        passport_normalized = f"Паспорт РФ: {passport_digits[:4]} {passport_digits[4:]}"

    elif document_type == "foreign_passport":
        if not re.match(r"^\d{9}$", passport_digits):
            return RedirectResponse(url="/admin/vouchers?error=bad_foreign_passport", status_code=303)

        passport_normalized = f"Загранпаспорт РФ: {passport_digits[:2]} {passport_digits[2:]}"

    else:
        return RedirectResponse(url="/admin/vouchers?error=bad_document_type", status_code=303)

    max_devices = max(1, min(int(max_devices), 20))
    valid_days = max(1, min(int(valid_days), 30))

    voucher = create_voucher(
        full_name=full_name,
        passport=passport_normalized,
        birth_date=birth_date.strip(),
        phone=phone.strip(),
        site=site.strip(),
        room_num=room_num.strip(),
        max_devices=max_devices,
        valid_days=valid_days,
        created_by=username,
    )

    audit(
        "admin_create_voucher",
        details=f"user={username}, voucher_id={voucher['id']}, max_devices={max_devices}, valid_days={valid_days}, site={site}, room={room_num}"
    )

    vid = int(voucher["id"])

    body = f"""
    <div class="voucher-created-wrap">
      <div class="card voucher-created-card">
        <h2>Ваучер создан</h2>

        <div class="voucher-created-code">
          {escape(voucher["code"])}
        </div>

        <div class="muted voucher-created-info">
          Устройств: {max_devices}<br>
          Действует до: {escape(str(voucher["valid_until"]))}
        </div>

        <div class="voucher-created-actions">
          <a class="btn" href="/admin/vouchers">Вернуться к ваучерам</a>
          <a class="btn primary" href="/admin/vouchers/{vid}/print" target="_blank">Печать</a>
        </div>
      </div>
    </div>
    """

    return admin_page("Ваучер создан", body, active_tab="vouchers", role=role)


@app.get("/admin/vouchers/{voucher_id}", response_class=HTMLResponse)
def admin_voucher_detail(request: Request, voucher_id: int):
    guard = role_guard(request, ("admin", "superadmin", "it", "reception"))
    if guard:
        return guard

    username, role = get_current_admin_user(request)

    conn = db()

    voucher = conn.execute("""
        SELECT *
        FROM vouchers
        WHERE id = ?
    """, (voucher_id,)).fetchone()

    if not voucher:
        audit(
            "admin_view_voucher_not_found",
            details=f"user={username}, voucher_id={voucher_id}"
        )
        conn.close()
        return admin_page(
            "Ваучер",
            "<div class='muted'>Ваучер не найден.</div>",
            active_tab="vouchers"
        )

    devices = conn.execute("""
        SELECT *
        FROM voucher_devices
        WHERE voucher_id = ?
        ORDER BY last_seen_at DESC
    """, (voucher_id,)).fetchall()

    conn.close()

    audit(
        "admin_view_voucher",
        details=f"user={username}, voucher_id={voucher_id}, site={voucher['site']}, room={voucher['room_num']}, status={voucher['status']}"
    )

    code = decrypt_voucher_code(voucher["code_enc"])

    status = voucher["status"] or ""
    if status == "active":
        status_html = '<span class="badge active">Активна</span>'
    elif status == "revoked":
        status_html = '<span class="badge blocked">Отключена</span>'
    elif status == "expired":
        status_html = '<span class="badge expired">Истекла</span>'
    else:
        status_html = f'<span class="badge">{escape(status)}</span>'

    device_rows = []
    for d in devices:
        device_rows.append({
            "mac": d["mac"],
            "ip": d["ip"],
            "first_seen": format_dt(d["first_seen_at"]),
            "last_seen": format_dt(d["last_seen_at"]),
            "": f"""
            <form method="post" action="/admin/vouchers/{voucher_id}/remove-device">
                <input type="hidden" name="mac" value="{d['mac']}">
                <button class="btn" style="height:32px;">Освободить</button>
            </form>
            """
        })


    revoke_btn = ""

    if role == "superadmin" and status == "active":
        revoke_btn = f"""
        <form method="post" action="/admin/vouchers/revoke" style="display:inline-flex; margin:0;"
              onsubmit="return confirm('Отключить ваучер? Дальнейший вход по нему будет запрещён.');">
          <input type="hidden" name="voucher_id" value="{voucher_id}">
          <button class="btn danger" type="submit">
            Отключить ваучер
          </button>
        </form>
        """

    body = f"""
    <div class="card" style="margin-bottom:16px;">
      <h2 style="margin:0 0 14px; font-size:26px;">Ваучер #{voucher_id}</h2>

      <div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px;">
        <div>
          <div class="muted">Код</div>
          <div style="font-size:24px; font-weight:900; letter-spacing:.05em;">{escape(code)}</div>
        </div>

        <div>
          <div class="muted">Статус</div>
          <div>{status_html}</div>
        </div>

        <div>
          <div class="muted">Устройств</div>
          <div style="font-weight:700;">{len(devices)} / {escape(str(voucher["max_devices"]))}</div>
        </div>

        <div>
          <div class="muted">Действует до</div>
          <div style="font-weight:700;">{escape(format_dt(voucher["valid_until"]))}</div>
        </div>

        <div>
          <div class="muted">Объект выдачи</div>
          <div style="font-weight:700;">{escape(str(voucher["site"] or "—"))}</div>
        </div>

        <div>
          <div class="muted">Комната</div>
          <div style="font-weight:700;">{escape(str(voucher["room_num"] or "—"))}</div>
        </div>
      </div>

      <div style="margin-top:18px; display:flex; gap:12px; flex-wrap:wrap;">
        <a class="btn voucher-back-btn" href="/admin/vouchers">← Назад к ваучерам</a>
        <a class="btn primary voucher-print-btn" href="/admin/vouchers/{voucher_id}/print" target="_blank">Печать</a>
        {revoke_btn}
      </div>

    <h2 style="margin:18px 0 12px; font-size:22px;">Подключенные устройства</h2>
    """

    if devices:
        body += """
        <div style="overflow-x:auto;">
          <table class="table">
            <thead>
              <tr>
                <th>MAC-адрес</th>
                <th>IP-адрес</th>
                <th>Подключено</th>
                <th>Последняя активность</th>
                <th>Действия</th>
              </tr>
            </thead>
            <tbody>
        """

        for d in devices:
            mac = str(d["mac"] or "")
            body += f"""
              <tr>
                <td>{escape(mac)}</td>
                <td>{escape(str(d["ip"] or ""))}</td>
                <td>{escape(format_dt(d["first_seen_at"]))}</td>
                <td>{escape(format_dt(d["last_seen_at"]))}</td>
                <td>
                  <form method="post" action="/admin/vouchers/{voucher_id}/remove-device" style="margin:0;"
                        onsubmit="return confirm('Освободить устройство {escape(mac)}?');">
                    <input type="hidden" name="mac" value="{escape(mac)}">
                    <button class="btn" type="submit">Освободить</button>
                  </form>
                </td>
              </tr>
            """

        body += """
            </tbody>
          </table>
        </div>
        """
    else:
        body += "<div class='muted'>По этому ваучеру пока нет подключенных устройств.</div>"
    

    return admin_page("Ваучер", body, active_tab="vouchers", role=role)

@app.get("/admin/vouchers/{voucher_id}/print", response_class=HTMLResponse)
def admin_voucher_print(request: Request, voucher_id: int):
    guard = role_guard(request, ("admin", "superadmin", "it", "reception"))
    if guard:
        return guard

    conn = db()
    voucher = conn.execute("""
        SELECT *
        FROM vouchers
        WHERE id = ?
    """, (voucher_id,)).fetchone()
    conn.close()

    if not voucher:
        return HTMLResponse("Ваучер не найден", status_code=404)

    code = decrypt_voucher_code(voucher["code_enc"])

    username, role = get_current_admin_user(request)

    audit(
        "admin_print_voucher",
        details=f"user={username}, voucher_id={voucher_id}, site={voucher['site']}, room={voucher['room_num']}"
    )

    return HTMLResponse(f"""
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>Ваучер {escape(code)}</title>
  <style>
    body {{
      margin: 0;
      font-family: Arial, sans-serif;
      background: #f3f4f6;
      color: #111827;
    }}

    .voucher-print {{
      width: 100mm;
      min-height: 70mm;
      margin: 8mm 0 0 8mm;
      padding: 7mm;
      border: 1px solid #d1d5db;
      border-radius: 10px;
      background: white;
      box-sizing: border-box;
    }}

    .brand {{
      font-size: 20px;
      font-weight: 800;
      margin-bottom: 4mm;
      text-align: center;
    }}

    .title {{
      font-size: 13px;
      text-align: center;
      margin-bottom: 5mm;
    }}

    .code {{
      font-size: 28px;
      font-weight: 900;
      letter-spacing: .08em;
      text-align: center;
      padding: 5mm;
      border: 1px dashed #9ca3af;
      border-radius: 8px;
      margin-bottom: 5mm;
    }}

    .row {{
      font-size: 12px;
      margin: 2mm 0;
    }}

    .muted {{
      font-size: 11px;
      margin-top: 4mm;
      color: #6b7280;
    }}

    .actions {{
      text-align: center;
      margin-top: 8mm;
    }}

    button {{
      padding: 10px 18px;
      border: 0;
      border-radius: 999px;
      background: #d6b34a;
      font-weight: 800;
      cursor: pointer;
    }}

    @media print {{
      body {{
        background: white;
      }}

      .voucher-print {{
        width: 100mm;
        min-height: 70mm;
        margin: 0;
        box-shadow: none;
      }}

      .actions {{
        display: none;
      }}

      @page {{
        size: A4 portrait;
        margin: 8mm;
      }}
    }}
  </style>
</head>
<body>
  <div class="voucher-print">
    <div class="brand">MIRACLEON WI-FI</div>
    <div class="title">Ваучер доступа в интернет</div>

    <div class="code">{escape(code)}</div>

    <div class="row"><b>Действует до:</b> {escape(format_dt(voucher["valid_until"]))}</div>
    <div class="row"><b>Устройств:</b> {escape(str(voucher["max_devices"]))}</div>
    <div class="row"><b>Объект:</b> {escape(str(voucher["site"] or "—"))}</div>
    <div class="row"><b>Комната:</b> {escape(str(voucher["room_num"] or "—"))}</div>

    <div class="row muted" style="margin-top:5mm;">
      Подключитесь к Wi-Fi MIRACLEON и выберите вход по ваучеру.
    </div>

    <div class="actions">
      <button onclick="window.print()">Распечатать</button>
    </div>
  </div>

  <script>
    window.addEventListener("load", () => {{
      setTimeout(() => window.print(), 300);
    }});
  </script>
</body>
</html>
""")

@app.post("/admin/vouchers/revoke")
def admin_vouchers_revoke(
    request: Request,
    voucher_id: int = Form(...),
):
    guard = role_guard(request, ("admin", "superadmin"))
    if guard:
        return guard

    username, role = get_current_admin_user(request)

    conn = db()
    row = conn.execute("""
        SELECT id, status, site, room_num
        FROM vouchers
        WHERE id = ?
    """, (voucher_id,)).fetchone()

    if not row:
        conn.close()
        audit(
            "admin_revoke_voucher_not_found",
            details=f"user={username}, voucher_id={voucher_id}"
        )
        return RedirectResponse(url="/admin/vouchers", status_code=303)

    conn.execute("""
        UPDATE vouchers
        SET status = 'revoked',
            revoked_at = ?,
            revoke_reason = 'manual_admin'
        WHERE id = ?
    """, (now_iso(), voucher_id))

    conn.commit()
    conn.close()

    audit(
        "admin_revoke_voucher",
        details=f"user={username}, voucher_id={voucher_id}, site={row['site']}, room={row['room_num']}, status_before={row['status']}"
    )

    return RedirectResponse(url="/admin/vouchers", status_code=303)



@app.post("/admin/vouchers/{voucher_id}/remove-device")
def admin_voucher_remove_device(
    request: Request,
    voucher_id: int,
    mac: str = Form(...),
):
    guard = role_guard(request, ("admin", "superadmin", "it", "reception"))
    if guard:
        return guard

    username, role = get_current_admin_user(request)
    mac = mac.strip()

    conn = db()
    row = conn.execute("""
        SELECT id, ip
        FROM voucher_devices
        WHERE voucher_id = ? AND mac = ?
    """, (voucher_id, mac)).fetchone()

    if row:
        device_ip = row["ip"]

        kicked = 0
        kick_error = ""

        try:
            kicked = disconnect_hotspot_active_by_mac(mac)
        except Exception as e:
            kick_error = str(e)

        conn.execute("""
            DELETE FROM voucher_devices
            WHERE voucher_id = ? AND mac = ?
        """, (voucher_id, mac))
        conn.commit()

        audit(
            "admin_voucher_remove_device",
            mac=mac,
            ip=device_ip,
            details=f"user={username}, voucher_id={voucher_id}, kicked={kicked}, kick_error={kick_error}"
        )

    conn.close()

    return RedirectResponse(url=f"/admin/vouchers/{voucher_id}", status_code=303)


@app.get("/admin/audit", response_class=HTMLResponse)
def admin_audit(request: Request):

    guard = role_guard(request, ("superadmin",))
    if guard:
        return guard

    username, role = get_current_admin_user(request)

    rows = fetch_all("SELECT * FROM audit_log ORDER BY event_time DESC LIMIT 20")
    cols = ["id", "phone", "mac", "ip", "nas_id", "hotel", "ssid", "vlan_id", "event_type", "event_time", "details"]
    body = html_table(rows, cols)
    return admin_page("Аудит", body, active_tab="audit", role=role)


@app.get("/admin/networks", response_class=HTMLResponse)
def admin_networks(request: Request, error: str = "", ok: str = "", edit_id: str = ""):
    guard = role_guard(request, ("superadmin",))
    if guard:
        return guard

    username, role = get_current_admin_user(request)

    body = build_networks_body(error=error, ok=ok, edit_id=edit_id)
    return admin_page("Сети", body, active_tab="networks")


def build_networks_body(error: str = "", ok: str = "", edit_id: str = "", cancel_url: str = "/admin/networks"):
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
      <form class="network-form" method="post" action="/admin/networks/add">
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
              <a class="btn" href="{cancel_url}">Отмена</a>
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
        toggle_text = "Отключить" if is_active == 1 else "Включить"

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
              <div class="table-actions">
                <a class="btn table-btn" href="/admin/networks?edit_id={rid}">Изменить</a>

                <form method="post" action="/admin/networks/toggle" style="margin:0;">
                  <input type="hidden" name="network_id" value="{rid}">
                  <button class="btn table-btn" type="submit">{toggle_text}</button>
                </form>

                <form method="post" action="/admin/networks/delete" style="margin:0;" onsubmit="return confirm('Удалить сеть? Это действие необратимо.');">
                  <input type="hidden" name="network_id" value="{rid}">
                  <button class="btn table-btn" type="submit">Удалить</button>
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
    return body        

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
    guard = role_guard(request, ("superadmin",))
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
    guard = role_guard(request, ("superadmin",))
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
    guard = role_guard(request, ("superadmin",))
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


@app.post("/admin/networks/delete")
def admin_networks_delete(
    request: Request,
    network_id: str = Form(""),
):
    guard = role_guard(request, ("superadmin",))
    if guard:
        return guard

    if not str(network_id).strip().isdigit():
        return RedirectResponse(url="/admin/networks?error=Некорректный ID сети", status_code=303)

    network_id_int = int(network_id)

    row = fetch_one("""
        SELECT id, hotel_name, ssid_name, vlan_id
        FROM network_map
        WHERE id = ?
    """, (network_id_int,))
    if not row:
        return RedirectResponse(url="/admin/networks?error=Сеть не найдена", status_code=303)

    hotel_name = (row["hotel_name"] or "").strip()
    ssid_name = (row["ssid_name"] or "").strip()
    vlan_id = str(row["vlan_id"] or "").strip()

    conn = db()

    session_ref = conn.execute("""
        SELECT 1
        FROM guest_sessions
        WHERE COALESCE(hotel, '') = ?
          AND COALESCE(ssid, '') = ?
          AND COALESCE(CAST(vlan_id AS TEXT), '') = ?
        LIMIT 1
    """, (hotel_name, ssid_name, vlan_id)).fetchone()

    pending_ref = conn.execute("""
        SELECT 1
        FROM pending_auth
        WHERE COALESCE(hotel, '') = ?
          AND COALESCE(ssid, '') = ?
          AND COALESCE(CAST(vlan_id AS TEXT), '') = ?
        LIMIT 1
    """, (hotel_name, ssid_name, vlan_id)).fetchone()

    if session_ref or pending_ref:
        conn.close()
        return RedirectResponse(
            url="/admin/networks?error=Нельзя удалить сеть: по ней уже есть связанные данные. Используйте отключение.",
            status_code=303
        )

    conn.execute("DELETE FROM network_map WHERE id = ?", (network_id_int,))
    conn.commit()
    conn.close()

    return RedirectResponse(url="/admin/networks?ok=Сеть удалена", status_code=303)


@app.get("/admin/find", response_class=HTMLResponse)
def admin_find(request: Request, q: str = ""):
    guard = role_guard(request, ("admin", "superadmin", "it"))
    if guard:
        return guard

    username, role = get_current_admin_user(request)

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
            active_tab="find",
            role=role
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
                "terminate_cause_raw",
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

    return admin_page("Поиск", body, active_tab="find", role=role)    


def build_system_tabs(section: str) -> str:
    section = (section or "export").strip()
    return f"""
    <div class="system-actions">
      <a class="btn {'primary' if section == 'export' else ''}" href="/admin/system?section=export">Выгрузка</a>
      <a class="btn {'primary' if section == 'networks' else ''}" href="/admin/system?section=networks">Сети</a>
      <a class="btn {'primary' if section == 'users' else ''}" href="/admin/system?section=users">Пользователи</a>
      <a class="btn {'primary' if section == 'settings' else ''}" href="/admin/system?section=settings">Настройки</a>
      <a class="btn {'primary' if section == 'logs' else ''}" href="/admin/system?section=logs">Логи</a>
      <a class="btn {'primary' if section == 'service' else ''}" href="/admin/system?section=service">Сервис</a>
    </div>
    """


@app.get("/admin/system", response_class=HTMLResponse)
def admin_system(request: Request, section: str = "export", password_id: str = "", ok: str = ""):
    guard = role_guard(request, ("superadmin",))
    if guard:
        return guard

    username, role = get_current_admin_user(request)

    section = (section or "export").strip()

    tabs = build_system_tabs(section)

    if section == "export":
        content = """
        <div class="system-section">
          <h2>Выгрузка данных</h2>

          <div class="toolbar">
            <form class="export-form" method="get" action="/admin/export/download">

              <div>
                <label style="display:block; margin-bottom:6px; font-weight:600;">Что выгружать</label>
                <select name="table_name">
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
                <select name="fmt">
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
        </div>
        """

    elif section == "networks":
        content = f"""
        <div class="system-section">
          <h2>Сети</h2>
          {build_networks_body(cancel_url="/admin/system?section=networks")}
        </div>
        """

    elif section == "users":
        users = fetch_all("""
            SELECT id, username, role, is_active, created_at, updated_at
            FROM admin_users
            ORDER BY id
        """)

        password_form = ""

        if str(password_id).strip().isdigit():
            pwd_user = fetch_one(
                "SELECT id, username FROM admin_users WHERE id = ?",
                (int(password_id),)
            )

            if pwd_user:
                password_form = f"""
                <div class="system-section" style="margin-bottom:16px;">
                  <h2>Смена пароля: {escape(str(pwd_user["username"]))}</h2>

                  <form class="users-form" method="post" action="/admin/system/users/password">
                    <input type="hidden" name="user_id" value="{int(pwd_user["id"])}">

                    <div>
                      <label style="display:block; margin-bottom:6px; font-weight:600;">Новый пароль</label>
                      <input type="password" name="password" required>
                    </div>

                    <div>
                      <button class="btn primary" type="submit">Сохранить пароль</button>
                    </div>

                    <div>
                      <a class="btn" href="/admin/system?section=users">Отмена</a>
                    </div>
                  </form>
                </div>
                """

        rows_html = ""

        for u in users:
            active_text = "Да" if int(u["is_active"]) == 1 else "Нет"
            toggle_text = "Отключить" if int(u["is_active"]) == 1 else "Включить"
            
            rows_html += f"""
            <tr>
              <td>{int(u["id"])}</td>
              <td>{escape(str(u["username"]))}</td>
              <td>{escape(str(u["role"]))}</td>
              <td>{active_text}</td>
              <td>{escape(format_dt(u["created_at"]))}</td>
              <td>{escape(format_dt(u["updated_at"]))}</td>

              <td>
                <div class="table-actions">
                  <a class="btn table-btn" href="/admin/system?section=users&password_id={int(u["id"])}">Пароль</a>

                  <form method="post" action="/admin/system/users/toggle" style="margin:0;">
                    <input type="hidden" name="user_id" value="{int(u["id"])}">
                    <button class="btn table-btn" type="submit">{toggle_text}</button>
                  </form>
                  <form method="post" action="/admin/system/users/delete" style="margin:0;" onsubmit="return confirm('Удалить пользователя?');">
                    <input type="hidden" name="user_id" value="{int(u["id"])}">
                    <button class="btn table-btn" type="submit">Удалить</button>
                  </form>
                </div>
              </td>
            </tr>
            """

        content = f"""
        <div class="system-section">
          <h2>Пользователи и роли</h2>

          <form class="users-form" method="post" action="/admin/system/users/add">
            <div>
              <label style="display:block; margin-bottom:6px; font-weight:600;">Логин</label>
              <input type="text" name="username" required>
            </div>

            <div>
              <label style="display:block; margin-bottom:6px; font-weight:600;">Пароль</label>
              <input type="password" name="password" required>
            </div>

            <div>
              <label style="display:block; margin-bottom:6px; font-weight:600;">Роль</label>
              <select name="role">
                <option value="reception">reception</option>
                <option value="it">it</option>
                <option value="superadmin">superadmin</option>
              </select>
            </div>

            <div>
              <button class="btn primary" type="submit">Добавить пользователя</button>
            </div>
          </form>

          {password_form}

          <div style="overflow-x:auto;">
            <table class="table">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Логин</th>
                  <th>Роль</th>
                  <th>Активен</th>
                  <th>Создан</th>
                  <th>Обновлён</th>
                  <th>Действия</th>
                </tr>
              </thead>
              <tbody>
                {rows_html}
              </tbody>
            </table>
          </div>
        </div>
        """

    elif section == "service":
        disk = get_disk_usage("/")
        memory = get_memory_usage()
        cpu = get_cpu_load()
        uptime = get_uptime()
        wal = get_wal_size()

        services = [
            ("Portal backend", "portal", "hotspot-captive-portal.service"),
            ("Cleanup worker", "cleanup", "hotspot-cleanup-worker.service"),
            ("MikroTik sync", "mikrotik", "hotspot-mikrotik-sync-worker.service"),
            ("Opera FIAS", "opera", "opera-fias-sync.service"),
            ("FreeRADIUS", "freeradius", "freeradius.service"),
        ]

        service_statuses = {
            key: systemctl_is_active(unit)
            for _, key, unit in services
        }

        service_rows = "".join(
            f"""
            <tr>
              <td>{escape(title)}</td>
              <td>{escape(unit)}</td>
              <td id="svc-status-{key}">{service_badge(service_statuses.get(key, "unknown"))}</td>
            </tr>
            """
            for title, key, unit in services
        )
        readiness_rows = build_readiness_rows(service_statuses)

        content = f"""
        <div class="system-section">
          <h2>Сервис</h2>

          <div class="table-wrap" style="margin-bottom:16px;">
            <table>
              <thead>
                <tr>
                  <th>Проверка готовности</th>
                  <th>Статус</th>
                  <th>Детали</th>
                </tr>
              </thead>
              <tbody>
                {readiness_rows}
              </tbody>
            </table>
          </div>

          <div class="system-stats system-stats-compact">
            <div class="stat">
              <div class="stat-label">CPU</div>
              <div class="stat-row">
                <div class="stat-value" id="svc-cpu">{cpu["percent"]}%</div>
                <div class="muted" id="svc-cpu-sub">load {cpu["load1"]} / {cpu["cores"]} cores</div>
              </div>
            </div>

            <div class="stat">
              <div class="stat-label">RAM</div>
              <div class="stat-row">
                <div class="stat-value" id="svc-ram">{memory["percent"]}%</div>
                <div class="muted" id="svc-ram-sub">{memory["used_h"]} / {memory["total_h"]}</div>
              </div>
            </div>

            <div class="stat">
              <div class="stat-label">Disk</div>
              <div class="stat-row">
                <div class="stat-value" id="svc-disk">{disk["percent"]}%</div>
                <div class="muted" id="svc-disk-sub">{disk["used_h"]} / {disk["total_h"]}</div>
              </div>
            </div>

            <div class="stat">
              <div class="stat-label">SQLite WAL</div>
              <div class="stat-row">
                <div class="stat-value" id="svc-wal">{wal["text"]}</div>
                <div class="muted">hotspot.db-wal</div>
              </div>
            </div>

            <div class="stat">
              <div class="stat-label">Uptime</div>
              <div class="stat-row">
                <div class="stat-value" id="svc-uptime">{uptime["text"]}</div>
                <div class="muted">сервер работает</div>
              </div>
            </div>
          </div>

          <div class="table-wrap" style="margin-top:16px;">
            <table>
              <thead>
                <tr>
                  <th>Компонент</th>
                  <th>Unit</th>
                  <th>Статус</th>
                </tr>
              </thead>
              <tbody>
                {service_rows}
              </tbody>
            </table>
          </div>

          <form method="post" action="/admin/system/service/restart"
                style="margin-top:16px;"
                onsubmit="return confirm('Перезапустить backend портала? Панель будет недоступна несколько секунд.');">
            <button class="btn primary" type="submit">Перезапустить портал</button>
          </form>
        </div>

        <script>
        function serviceBadge(status) {{
          let cls = "pending";
          if (status === "active") cls = "active";
          if (status === "failed") cls = "error";
          if (status === "inactive") cls = "muted";
          return '<span class="badge ' + cls + '">' + status + '</span>';
        }}

        async function refreshServiceStatus() {{
          try {{
            const resp = await fetch("/admin/system/service/status-json", {{cache: "no-store"}});
            if (!resp.ok) return;

            const data = await resp.json();
            if (!data.ok) return;

            document.getElementById("svc-cpu").textContent = data.cpu.percent + "%";
            document.getElementById("svc-cpu-sub").textContent = "load " + data.cpu.load1 + " / " + data.cpu.cores + " cores";

            document.getElementById("svc-ram").textContent = data.memory.percent + "%";
            document.getElementById("svc-ram-sub").textContent = data.memory.used_h + " / " + data.memory.total_h;

            document.getElementById("svc-disk").textContent = data.disk.percent + "%";
            document.getElementById("svc-disk-sub").textContent = data.disk.used_h + " / " + data.disk.total_h;

            document.getElementById("svc-wal").textContent = data.wal.text;
            document.getElementById("svc-uptime").textContent = data.uptime.text;

            for (const [key, status] of Object.entries(data.services)) {{
              const el = document.getElementById("svc-status-" + key);
              if (el) el.innerHTML = serviceBadge(status);
            }}
          }} catch (e) {{}}
        }}

        setInterval(refreshServiceStatus, 15000);
        </script>
        """

    elif section == "settings":
        content = build_settings_body(ok=ok)


    elif section == "logs":
        unit = request.query_params.get("unit", "portal")
        lines = request.query_params.get("lines", "100")
        level = request.query_params.get("level", "all")

        q = request.query_params.get("q", "").strip()

        log_text = read_service_logs(unit, int(lines), level=level, newest_first=False)

        if q:
            q_lower = q.lower()
            log_text = "\n".join(
                line for line in log_text.splitlines()
                if q_lower in line.lower()
            )
                        
        content = f"""
        <div class="system-section">
          <h2>Логи</h2>

          <form method="get" action="/admin/system" class="export-form">
            <input type="hidden" name="section" value="logs">

            <div>
              <label style="display:block; margin-bottom:6px; font-weight:600;">Сервис</label>
              <select name="unit">
                <option value="portal" {"selected" if unit == "portal" else ""}>Portal</option>
                <option value="cleanup" {"selected" if unit == "cleanup" else ""}>Cleanup</option>
                <option value="mikrotik" {"selected" if unit == "mikrotik" else ""}>MikroTik sync</option>
                <option value="opera" {"selected" if unit == "opera" else ""}>Opera FIAS</option>
              </select>
            </div>

            <div>
              <label style="display:block; margin-bottom:6px; font-weight:600;">Строк</label>
              <select name="lines">
                <option value="50" {"selected" if str(lines) == "50" else ""}>50</option>
                <option value="100" {"selected" if str(lines) == "100" else ""}>100</option>
                <option value="200" {"selected" if str(lines) == "200" else ""}>200</option>
                <option value="300" {"selected" if str(lines) == "300" else ""}>300</option>
              </select>
            </div>

            <div>
              <label style="display:block; margin-bottom:6px; font-weight:600;">Фильтр</label>
              <select name="level">
                <option value="all" {"selected" if level == "all" else ""}>Все</option>
                <option value="error" {"selected" if level == "error" else ""}>Ошибки</option>
              </select>
            </div>

            <div>
              <label style="display:block; margin-bottom:6px; font-weight:600;">Поиск</label>
              <input type="text" name="q" id="log-search" value="{escape(q)}" placeholder="телефон, MAC, IP, PHONE_, vlan">
            </div>

            <div>
              <button class="btn primary" type="submit">Открыть</button>
            </div>
          </form>

          <div class="muted" style="margin:10px 0 12px;">
            Старые записи загружены при открытии страницы. Новые строки добавляются ниже в реальном времени.
          </div>

          <pre id="live-log-box" style="white-space:pre-wrap; word-break:break-word; background:#111827; color:#e5e7eb; padding:14px; border-radius:12px; overflow:auto; max-height:650px;">{escape(log_text)}</pre>
        </div>

        <script>
        (function () {{
          const box = document.getElementById("live-log-box");
          const unit = "{escape(unit)}";
          const level = "{escape(level)}";
          const query = "{escape(q)}".toLowerCase();

          const url = "/admin/system/logs-stream?unit=" + encodeURIComponent(unit) + "&level=" + encodeURIComponent(level);

          const es = new EventSource(url);

          function appendLine(line) {{
            if (!box) return;

            if (query) {{
              const lowLine = line.toLowerCase();
              if (!lowLine.includes(query)) return;
            }}

            const nearBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
            box.textContent += "\\n" + line;

            const maxChars = 120000;
            if (box.textContent.length > maxChars) {{
              box.textContent = box.textContent.slice(-maxChars);
            }}

            if (nearBottom) {{
              box.scrollTop = box.scrollHeight;
            }}
          }}

          es.onmessage = function (event) {{
            appendLine(event.data);
          }};

          es.onerror = function () {{
            appendLine("[live-log] соединение потеряно, браузер попробует переподключиться...");
          }};

          if (box) {{
            box.scrollTop = box.scrollHeight;
          }}
        }})();
        </script>
        """

    else:
        return RedirectResponse(url="/admin/system?section=export", status_code=303)

    body = tabs + content

    return admin_page("Система", body, active_tab="system")
    

@app.get("/admin/system/logs-stream")
def admin_system_logs_stream(request: Request, unit: str = "portal", level: str = "all"):
    guard = role_guard(request, ("superadmin", "it"))
    if guard:
        return guard

    service = resolve_log_service(unit)
    level = (level or "all").strip().lower()

    def should_emit(line: str) -> bool:
        if level != "error":
            return True

        low = line.lower()
        return (
            "error" in low
            or "exception" in low
            or "traceback" in low
            or "failed" in low
        )

    def stream():
        cmd = ["journalctl", "-u", service, "-f", "-n", "0", "--no-pager", "-l"]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        yield f"data: [live-log] connected to {service}\n\n"

        try:
            while True:
                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        yield f"data: [live-log] journalctl stopped with code {proc.returncode}\n\n"
                        break
                    continue

                line = line.rstrip("\n")
                if not should_emit(line):
                    continue

                line = line.replace("\r", "")
                yield f"data: {line}\n\n"

        finally:
            proc.terminate()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/admin/system/users/add")
def admin_system_users_add(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
):
    guard = role_guard(request, ("superadmin",))
    if guard:
        return guard

    username = username.strip()
    role = role.strip()

    if role not in ("superadmin", "it", "reception"):
        return RedirectResponse(url="/admin/system?section=users&error=bad_role", status_code=303)

    if not username or not password:
        return RedirectResponse(url="/admin/system?section=users&error=empty", status_code=303)

    now = datetime.now(timezone.utc).isoformat()

    try:
        conn = db()
        conn.execute("""
            INSERT INTO admin_users (
                username, password_hash, role, is_active, created_at, updated_at
            )
            VALUES (?, ?, ?, 1, ?, ?)
        """, (
            username,
            hash_admin_password(password),
            role,
            now,
            now
        ))
        conn.commit()
        conn.close()
    except Exception:
        return RedirectResponse(url="/admin/system?section=users&error=exists", status_code=303)

    return RedirectResponse(url="/admin/system?section=users&ok=created", status_code=303)


@app.post("/admin/system/users/toggle")
def admin_system_users_toggle(
    request: Request,
    user_id: int = Form(...)
):
    guard = role_guard(request, ("superadmin",))
    if guard:
        return guard

    user = fetch_one(
        "SELECT * FROM admin_users WHERE id = ?",
        (user_id,)
    )

    if not user:
        return RedirectResponse(
            url="/admin/system?section=users",
            status_code=303
        )

    new_state = 0 if int(user["is_active"]) == 1 else 1

    conn = db()
    conn.execute("""
        UPDATE admin_users
        SET is_active = ?, updated_at = ?
        WHERE id = ?
    """, (
        new_state,
        datetime.now(timezone.utc).isoformat(),
        user_id
    ))
    conn.commit()
    conn.close()

    return RedirectResponse(url="/admin/system?section=users&ok=updated", status_code=303)


@app.post("/admin/system/users/delete")
def admin_system_users_delete(
    request: Request,
    user_id: int = Form(...)
):
    guard = role_guard(request, ("superadmin",))
    if guard:
        return guard

    current_username, current_role = get_current_admin_user(request)

    user = fetch_one(
        "SELECT id, username, role, is_active FROM admin_users WHERE id = ?",
        (user_id,)
    )

    if not user:
        return RedirectResponse(url="/admin/system?section=users", status_code=303)

    if str(user["username"]) == str(current_username):
        return RedirectResponse(url="/admin/system?section=users&error=self_delete", status_code=303)

    if str(user["role"]) == "superadmin":
        cnt = fetch_one("""
            SELECT COUNT(*) AS cnt
            FROM admin_users
            WHERE role = 'superadmin'
              AND is_active = 1
        """)["cnt"]

        if int(cnt) <= 1:
            return RedirectResponse(url="/admin/system?section=users&error=last_superadmin", status_code=303)

    conn = db()
    conn.execute(
        "DELETE FROM admin_users WHERE id = ?",
        (user_id,)
    )
    conn.commit()
    conn.close()


    return RedirectResponse(url="/admin/system?section=users&ok=deleted", status_code=303)


@app.post("/admin/system/users/password")
def admin_system_users_password(
    request: Request,
    user_id: int = Form(...),
    password: str = Form(...)
):
    guard = role_guard(request, ("superadmin",))
    if guard:
        return guard

    password = password.strip()

    if len(password) < 6:
        return RedirectResponse(url=f"/admin/system?section=users&password_id={user_id}&error=short_password", status_code=303)

    user = fetch_one(
        "SELECT id FROM admin_users WHERE id = ?",
        (user_id,)
    )

    if not user:
        return RedirectResponse(url="/admin/system?section=users&error=user_not_found", status_code=303)

    conn = db()
    conn.execute("""
        UPDATE admin_users
        SET password_hash = ?, updated_at = ?
        WHERE id = ?
    """, (
        hash_admin_password(password),
        datetime.now(timezone.utc).isoformat(),
        user_id
    ))
    conn.commit()
    conn.close()

    return RedirectResponse(url="/admin/system?section=users&ok=password_changed", status_code=303)


@app.get("/admin/export/xlsx/{table_name}")
def admin_export_xlsx(
    request: Request,
    table_name: str,
    date_from: str | None = None,
    date_to: str | None = None
):
    guard = role_guard(request, ("superadmin",))
    if guard:
        return guard

    username, role = get_current_admin_user(request)

    data = build_single_xlsx(table_name, date_from=date_from, date_to=date_to)
    filename = f"{table_name}_{date_from or 'all'}_{date_to or 'all'}.xlsx"

    return StreamingResponse(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

@app.get("/admin/export", response_class=HTMLResponse)
def admin_export_page(request: Request):
    guard = role_guard(request, ("superadmin",))
    if guard:
        return guard

    username, role = get_current_admin_user(request)

    body = """
    <div class="toolbar">
      <form class="export-form" method="get" action="/admin/export/download">
        <div>
          <label style="display:block; margin-bottom:6px; font-weight:600;">Что выгружать</label>
          <select name="table_name">
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
          <select name="fmt">
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
    guard = role_guard(request, ("superadmin",))
    if guard:
        return guard

    username, role = get_current_admin_user(request)

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
    guard = role_guard(request, ("admin", "superadmin", "it"))
    if guard:
        return guard

    username, role = get_current_admin_user(request)

    guests_cnt = fetch_all("SELECT COUNT(*) AS cnt FROM guests")[0]["cnt"]
    sessions_cnt = fetch_all("SELECT COUNT(*) AS cnt FROM guest_sessions WHERE status='active' AND ended_at IS NULL AND datetime(last_seen_at) >= datetime('now', '-15 minutes')")[0]["cnt"]
    pending_cnt = fetch_all("SELECT COUNT(*) AS cnt FROM pending_auth WHERE status='pending'")[0]["cnt"]

    auth_call_today = fetch_all("""
        SELECT COUNT(*) AS cnt
        FROM audit_log
        WHERE event_type = 'call_verified'
          AND date(datetime(event_time, '+3 hours')) = date(datetime('now', '+3 hours'))
    """)[0]["cnt"]

    auth_voucher_today = fetch_all("""
        SELECT COUNT(*) AS cnt
        FROM audit_log
        WHERE event_type = 'radius_accept_voucher'
          AND date(datetime(event_time, '+3 hours')) = date(datetime('now', '+3 hours'))
    """)[0]["cnt"]

    auth_room_today = fetch_all("""
        SELECT COUNT(*) AS cnt
        FROM (
            SELECT hotel, room_num, lower(trim(surname)) AS surname_norm
            FROM guest_sessions
            WHERE auth_method = 'room'
              AND date(datetime(started_at, '+3 hours')) = date(datetime('now', '+3 hours'))
              AND room_num IS NOT NULL
              AND room_num != ''
              AND surname IS NOT NULL
              AND surname != ''
            GROUP BY hotel, room_num, lower(trim(surname))
        ) x
    """)[0]["cnt"]

    now_local = datetime.now(DISPLAY_TZ)

    def parse_dt(value):
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(DISPLAY_TZ)
        except Exception:
            return None

    buckets = []

    if period == "1h":
        current = now_local.replace(second=0, microsecond=0)
        minute_floor = current.minute - (current.minute % 5)
        end = current.replace(minute=minute_floor)
        start = end - timedelta(minutes=55)

        for i in range(12):
            b_start = start + timedelta(minutes=i * 5)
            b_end = b_start + timedelta(minutes=5)
            buckets.append({
                "label": b_start.strftime("%H:%M"),
                "start": b_start,
                "end": b_end,
            })

    elif period == "1mo":
        start_day = now_local.date() - timedelta(days=29)
        for i in range(30):
            d = start_day + timedelta(days=i)
            b_start = datetime(d.year, d.month, d.day, tzinfo=DISPLAY_TZ)
            b_end = b_start + timedelta(days=1)
            buckets.append({
                "label": b_start.strftime("%d.%m"),
                "start": b_start,
                "end": b_end,
            })

    elif period == "1y":
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
            b_start = datetime(y, m, 1, tzinfo=DISPLAY_TZ)
            if m == 12:
                b_end = datetime(y + 1, 1, 1, tzinfo=DISPLAY_TZ)
            else:
                b_end = datetime(y, m + 1, 1, tzinfo=DISPLAY_TZ)

            buckets.append({
                "label": b_start.strftime("%m.%Y"),
                "start": b_start,
                "end": b_end,
            })

    else:
        period = "1d"
        start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

        for h in range(24):
            b_start = start + timedelta(hours=h)
            b_end = b_start + timedelta(hours=1)
            buckets.append({
                "label": f"{h:02d}:00",
                "start": b_start,
                "end": b_end,
            })

    labels = [b["label"] for b in buckets]

    active_sessions = [0 for _ in buckets]
    auth_call = [0 for _ in buckets]
    auth_voucher = [0 for _ in buckets]
    auth_room = [0 for _ in buckets]

    range_start = buckets[0]["start"]
    range_end = buckets[-1]["end"]

    session_rows = fetch_all("""
        SELECT started_at, ended_at, last_seen_at, status
        FROM guest_sessions
        WHERE started_at IS NOT NULL
          AND (
            ended_at IS NOT NULL
            OR status = 'active'
          )
    """)

    for row in session_rows:
        started = parse_dt(row["started_at"])
        ended = parse_dt(row["ended_at"])

        if not started:
            continue

        if not ended and row["status"] != "active":
            ended = parse_dt(row["last_seen_at"])

        if not ended and row["status"] != "active":
            continue

        if started >= range_end:
            continue

        if ended and ended <= range_start:
            continue

        for idx, b in enumerate(buckets):
            if started < b["end"] and (ended is None or ended > b["start"]):
                active_sessions[idx] += 1

    event_rows = fetch_all("""
    SELECT event_type, event_time
    FROM audit_log
    WHERE event_type IN (
        'call_verified',
        'radius_accept_voucher'
    )
    """)

    for row in event_rows:
        event_time = parse_dt(row["event_time"])
        if not event_time:
            continue

        if event_time < range_start or event_time >= range_end:
            continue

        for idx, b in enumerate(buckets):
            if b["start"] <= event_time < b["end"]:
                if row["event_type"] == "call_verified":
                    auth_call[idx] += 1
                elif row["event_type"] == "radius_accept_voucher":
                    auth_voucher[idx] += 1
                break

    room_rows = fetch_all("""
        SELECT started_at, hotel, room_num, surname
        FROM guest_sessions
        WHERE auth_method = 'room'
          AND started_at IS NOT NULL
          AND room_num IS NOT NULL
          AND room_num != ''
          AND surname IS NOT NULL
          AND surname != ''
    """)

    room_seen = [set() for _ in buckets]

    for row in room_rows:
        started = parse_dt(row["started_at"])

        if not started:
            continue

        if started < range_start or started >= range_end:
            continue

        guest_key = (
            (row["hotel"] or "").strip().lower(),
            (row["room_num"] or "").strip().lower(),
            (row["surname"] or "").strip().lower(),
        )

        for idx, b in enumerate(buckets):
            if b["start"] <= started < b["end"]:
                room_seen[idx].add(guest_key)
                break

    auth_room = [len(s) for s in room_seen]

    for row in event_rows:
        event_time = parse_dt(row["event_time"])
        if not event_time:
            continue

        if event_time < range_start or event_time >= range_end:
            continue

        for idx, b in enumerate(buckets):
            if b["start"] <= event_time < b["end"]:
                if row["event_type"] == "call_verified":
                    auth_call[idx] += 1
                elif row["event_type"] == "radius_accept_voucher":
                    auth_voucher[idx] += 1
                elif row["event_type"] == "radius_accept_room_auth":
                    auth_room[idx] += 1
                break

    auth_total = [
        auth_call[i] + auth_voucher[i] + auth_room[i]
        for i in range(len(buckets))
    ]

    hotspot_sites = []

    try:
        hotspot_rows = fetch_hotspot_active()

        server_labels = {
            "great_hall": "Great Hall",
            "fioleto": "FioLeto",
            "gorod_mira": "Gorod Mira",
            "movenpick": "Movenpick",
            "funf": "Funf",
            "dusit": "Dusit",
        }

        site_counts = {}

        for row in hotspot_rows:
            server = row.get("server") or "unknown"
            label = server_labels.get(server, server)
            site_counts[label] = site_counts.get(label, 0) + 1

        hotspot_sites = [
            {"name": name, "active": active}
            for name, active in sorted(site_counts.items())
        ]

    except Exception as e:
        logger.warning("hotspot active fetch failed: %s", e)
        hotspot_sites = []

    return {
        "stats": {
            "guests": guests_cnt,
            "active_sessions": sessions_cnt,
            "pending": pending_cnt,
            "calls_today": auth_call_today,
            "auth_call_today": auth_call_today,
            "auth_voucher_today": auth_voucher_today,
            "auth_room_today": auth_room_today,
        },
        "hotspot_sites": hotspot_sites,
        "chart": {
            "labels": labels,
            "active_sessions": active_sessions,
            "auth_total": auth_total,
            "auth_call": auth_call,
            "auth_voucher": auth_voucher,
            "auth_room": auth_room,
            "period": period,
        }
    }


@app.get("/admin/client", response_class=HTMLResponse)
def admin_client(
    request: Request,
    phone: str = "",
    mac: str = "",
    guest_id: int = 0,
):
    guard = role_guard(request, ("admin", "superadmin", "it"))
    if guard:
        return guard

    username, role = get_current_admin_user(request)

    phone = phone.strip()
    mac = mac.strip().lower()

    guest = None

    if guest_id:
        guest_rows = fetch_all(
            "SELECT * FROM guests WHERE id = ? LIMIT 1",
            (guest_id,)
        )
        if guest_rows:
            guest = guest_rows[0]
            phone = str(guest["phone"] or "").strip()

    normalized_phone = None

    if phone:
        try:
            normalized_phone = normalize_phone(phone)
        except Exception:
            normalized_phone = phone

    if not phone and not mac and not guest_id:
        return admin_page(
            "Карточка клиента",
            '<div class="muted">Не указан номер телефона или MAC-адрес.</div>',
            active_tab="sessions"
        )

    where = []
    params = []

    if guest_id:
        where.append("guest_id = ?")
        params.append(guest_id)
    else:
        if normalized_phone:
            where.append("phone = ?")
            params.append(normalized_phone)

        if mac:
            where.append("LOWER(mac) = ?")
            params.append(mac)

    where_sql = " OR ".join(where)

    sessions = fetch_all(
        f"""
        SELECT *
        FROM guest_sessions
        WHERE {where_sql}
        ORDER BY started_at DESC
        LIMIT 20
        """,
        tuple(params)
    )

    if not sessions:
        return admin_page(
            "Карточка клиента",
            '<div class="muted">Сессии по указанным данным не найдены.</div>',
            active_tab="sessions"
        )

    first = sessions[0]

    if first["auth_method"] == "room":
        client_label = f'Комната {first["room_num"] or ""} — {first["surname"] or ""}'
    else:
        client_label = phone or (first["phone"] or "")

    client_mac = mac or (first["mac"] or "")

    unique_macs = sorted({(row["mac"] or "").strip() for row in sessions if (row["mac"] or "").strip()})
    unique_hotels = sorted({(row["hotel"] or "").strip() for row in sessions if (row["hotel"] or "").strip()})
    unique_ssids = sorted({(row["ssid"] or "").strip() for row in sessions if (row["ssid"] or "").strip()})
    unique_ips = sorted({(row["ip"] or "").strip() for row in sessions if (row["ip"] or "").strip()})
    active_count = sum(1 for row in sessions if (row["status"] or "") == "active")

    total_session_time = 0
    for row in sessions:
        try:
            total_session_time += int(row["acct_session_time"] or 0)
        except Exception:
            pass

    def fmt_duration(seconds: int) -> str:
        seconds = max(0, int(seconds))
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, secs = divmod(rem, 60)
        if days:
            return f"{days}д {hours}ч {minutes}м"
        if hours:
            return f"{hours}ч {minutes}м"
        if minutes:
            return f"{minutes}м {secs}с"
        return f"{secs}с"

    summary_html = f"""
    <div class="toolbar" style="margin-bottom:16px; display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:12px;">
      <div class="card" style="padding:14px;">
        <div class="muted">Клиент</div>
        <div style="font-size:18px; font-weight:700;">{escape(client_label or '—')}</div>
      </div>
      <div class="card" style="padding:14px;">
        <div class="muted">Активных сессий</div>
        <div style="font-size:18px; font-weight:700;">{active_count}</div>
      </div>
      <div class="card" style="padding:14px;">
        <div class="muted">Всего сессий</div>
        <div style="font-size:18px; font-weight:700;">{len(sessions)}</div>
      </div>
      <div class="card" style="padding:14px;">
        <div class="muted">Суммарное время</div>
        <div style="font-size:18px; font-weight:700;">{fmt_duration(total_session_time)}</div>
      </div>
    </div>

    <div class="toolbar" style="margin-bottom:16px; display:grid; grid-template-columns:repeat(auto-fit, minmax(260px, 1fr)); gap:12px;">
      <div class="card" style="padding:14px;">
        <div class="muted" style="margin-bottom:6px;">MAC-адреса</div>
        <div>{'<br>'.join(escape(x) for x in unique_macs) if unique_macs else '—'}</div>
      </div>
      <div class="card" style="padding:14px;">
        <div class="muted" style="margin-bottom:6px;">Объекты</div>
        <div>{'<br>'.join(escape(x) for x in unique_hotels) if unique_hotels else '—'}</div>
      </div>
      <div class="card" style="padding:14px;">
        <div class="muted" style="margin-bottom:6px;">Wi-Fi сети</div>
        <div>{'<br>'.join(escape(x) for x in unique_ssids) if unique_ssids else '—'}</div>
      </div>
      <div class="card" style="padding:14px;">
        <div class="muted" style="margin-bottom:6px;">IP-адреса</div>
        <div>{'<br>'.join(escape(x) for x in unique_ips) if unique_ips else '—'}</div>
      </div>
    </div>
    """

    
    linked_sessions = []
    for row in sessions:
        row_dict = dict(row)

        if row["auth_method"] == "room":
            row_dict["phone"] = f'Комната {row["room_num"] or ""} — {row["surname"] or ""}'
        else:
            row_dict["phone"] = str(row["phone"] or "")

        linked_sessions.append(row_dict)


    body = summary_html
    body += html_table(
        linked_sessions,
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
            "terminate_cause_raw",
            "acct_session_time",
            "hotel",
            "ssid",
            "vlan_id",
            "nas_id",
            "acct_session_id",
        ]
    )

    return admin_page("Карточка клиента", body, active_tab="sessions", role=role)


@app.post("/auth/dusit/room")
def auth_dusit_room(request: Request, payload: dict = Body(...)):
    pms_api_guard(request)

    room_num = (payload.get("room_num") or "").strip()
    surname = (payload.get("surname") or "").strip()

    if not room_num or not surname:
        raise HTTPException(status_code=400, detail="room_num_and_surname_required")
    pms_result = pms_room_auth_allowed(room_num, surname, hotel="Dusit")
    if not pms_result.get("ok"):
        return {
            "ok": False,
            "status": "not_found",
            "error": pms_result.get("error") or "",
            "source": pms_result.get("source"),
        }

    return {
        "ok": True,
        "status": "ok",
        "source": pms_result.get("source"),
    }


@app.post("/admin/system/service/restart")
def admin_service_restart(request: Request):
    guard = role_guard(request, ("superadmin",))
    if guard:
        return guard

    subprocess.Popen(
        ["/usr/bin/sudo", "/usr/local/sbin/hotspot-portal-restart"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    return HTMLResponse("""
    <!doctype html>
    <html lang="ru">
    <head>
      <meta charset="utf-8">
      <meta http-equiv="refresh" content="5;url=/admin/system?section=service">
      <link rel="stylesheet" href="/static/admin.css">
      <title>Перезапуск</title>
    </head>
    <body class="login-page">
      <div class="login-wrap">
        <div class="login-card">
          <div class="login-brand">MIRACLEON WI-FI</div>
          <h1>Портал перезапускается</h1>
          <div class="login-subtitle">Через несколько секунд страница обновится.</div>
        </div>
      </div>
    </body>
    </html>
    """)


def resolve_log_service(unit: str) -> str:
    allowed_units = {
        "portal": "hotspot-captive-portal.service",
        "cleanup": "hotspot-cleanup-worker.service",
        "mikrotik": "hotspot-mikrotik-sync-worker.service",
        "opera": "opera-fias-sync.service",
    }
    return allowed_units.get(unit, allowed_units["portal"])

def read_service_logs(unit: str, lines: int = 100, level: str = "all", newest_first: bool = True) -> str:
    service = resolve_log_service(unit)
    lines = max(20, min(int(lines or 100), 300))
    level = (level or "all").strip().lower()

    try:
        r = subprocess.run(
            ["journalctl", "-u", service, "-n", str(lines), "--no-pager", "-l"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        log_text = r.stdout or r.stderr or ""
    except Exception as e:
        return f"Ошибка чтения логов: {e}"

    log_lines = log_text.splitlines()

    if level == "error":
        log_lines = [
            line for line in log_lines
            if "error" in line.lower()
            or "exception" in line.lower()
            or "traceback" in line.lower()
            or "failed" in line.lower()
        ]

    if newest_first:
        log_lines.reverse()

    return "\n".join(log_lines)
