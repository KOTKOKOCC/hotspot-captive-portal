from datetime import datetime, timezone
from html import escape
from urllib.parse import quote_plus

from fastapi.responses import HTMLResponse

from labels import (
    COLUMN_LABELS,
    EVENT_LABELS,
    RESULT_LABELS,
    AUTH_METHOD_LABELS,
    STATUS_LABELS,
    TERMINATE_CAUSE_LABELS,
)
from services import DISPLAY_TZ


def format_dt(value):
    if value is None:
        return ""

    if not isinstance(value, str):
        return str(value)

    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    dt = dt.astimezone(DISPLAY_TZ)
    return dt.strftime("%d.%m.%Y %H:%M:%S")


def humanize_details(value: str) -> str:
    if not value:
        return ""

    replacements = {
        "Pending created on first radius-check": "Пользователь ввёл номер и ожидает звонка",
        "Phone verified by PBX call": "Номер подтверждён через PBX",
        "PBX call without pending auth": "Входящий звонок без активной заявки",
        "Pending expired": "Срок ожидания звонка истёк",
    }

    for old, new in replacements.items():
        value = value.replace(old, new)

    value = value.replace("Session id=", "ID сессии: ")
    value = value.replace("guest_id=", "ID гостя: ")
    value = value.replace("Guest id=", "ID гостя: ")

    return value


def html_table(rows, columns):
    datetime_columns = {
        "created_at",
        "updated_at",
        "first_verified_at",
        "started_at",
        "ended_at",
        "expires_at",
        "event_time",
        "last_seen_at",
        "last_auth_at",
    }

    def fmt_status(value):
        raw = str(value or "")
        val = raw.lower()
        label = STATUS_LABELS.get(val, raw)

        cls = ""
        if val in {"active", "matched_pending"}:
            cls = "active"
        elif val in {"pending"}:
            cls = "pending"
        elif val in {"expired", "closed"}:
            cls = "expired"
        elif val in {"blocked", "no_pending", "invalid"}:
            cls = "error"

        return f'<span class="badge {cls}">{escape(label)}</span>'

    parts = ['<div class="table-wrap"><table>']
    parts.append(
        "<tr>" +
        "".join(f"<th>{escape(COLUMN_LABELS.get(col, col))}</th>" for col in columns) +
        "</tr>"
    )

    for row in rows:
        cells = []
        for col in columns:
            value = row[col] if row[col] is not None else ""

            if col in datetime_columns:
                value = format_dt(value)

            elif col == "event_type":
                value = EVENT_LABELS.get(str(value), str(value))

            elif col == "result":
                value = RESULT_LABELS.get(str(value), str(value))

            elif col == "details":
                value = humanize_details(str(value))

            elif col == "auth_method":
                value = AUTH_METHOD_LABELS.get(str(value), str(value))

            elif col == "terminate_cause":
                value = TERMINATE_CAUSE_LABELS.get(str(value), str(value))

            elif col == "status":
                cells.append(f"<td>{fmt_status(value)}</td>")
                continue

            elif col == "phone" and value:
                phone_link = f'/admin/client?phone={quote_plus(str(value))}'
                cells.append(f'<td><a href="{phone_link}">{escape(str(value))}</a></td>')
                continue

            elif col == "mac" and value:
                mac_link = f'/admin/client?mac={quote_plus(str(value))}'
                cells.append(f'<td><a href="{mac_link}">{escape(str(value))}</a></td>')
                continue
                
            cells.append(f"<td>{escape(str(value))}</td>")

        parts.append("<tr>" + "".join(cells) + "</tr>")

    parts.append("</table></div>")
    return "".join(parts)



def admin_page(title: str, body: str, active_tab: str = "") -> HTMLResponse:
    def nav_item(href: str, label: str, key: str) -> str:
        cls = "active" if active_tab == key else ""
        return f'<a href="{href}" class="{cls}">{label}</a>'

    nav_html = "".join([
        nav_item("/admin", "Главная", "home"),
        nav_item("/admin/guests", "Гости", "guests"),
        nav_item("/admin/sessions", "Сессии", "sessions"),
        nav_item("/admin/pending", "Ожидают подтверждения", "pending"),
        nav_item("/admin/calls", "Звонки", "calls"),
        nav_item("/admin/audit", "Аудит", "audit"),
        nav_item("/admin/networks", "Сети", "networks"),
        nav_item("/admin/find", "Поиск", "find"),
        nav_item("/admin/export", "Выгрузка", "export"),
        nav_item("/admin/logout", "Выход", "logout"),
    ])
    html = f"""
    <!doctype html>
    <html lang="ru">
    <head>
      <meta charset="utf-8">
      <title>{escape(title)}</title>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <style>
        :root {{
          --bg: #f4f7fb;
          --card: #ffffff;
          --line: #e5e7eb;
          --text: #1f2937;
          --muted: #6b7280;
          --blue: #2563eb;
          --blue-soft: #dbeafe;
          --green: #16a34a;
          --green-soft: #dcfce7;
          --yellow: #ca8a04;
          --yellow-soft: #fef9c3;
          --red: #dc2626;
          --red-soft: #fee2e2;
          --shadow: 0 10px 30px rgba(0,0,0,.06);
          --radius: 16px;
        }}

        * {{ box-sizing: border-box; }}

        body {{
          margin: 0;
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
          background: var(--bg);
          color: var(--text);
        }}

        .wrap {{
          width: calc(100vw - 64px);
          max-width: none;
          margin: 0 auto;
          padding: 16px;
        }}

        .topbar {{
          display: flex;
          justify-content: space-between;
          align-items: center;
          gap: 16px;
          margin-bottom: 18px;
        }}

        .brand {{
          font-size: 30px;
          font-weight: 800;
        }}

        .subtitle {{
          color: var(--muted);
          font-size: 14px;
          margin-top: 4px;
        }}

        .nav {{
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
          gap: 10px;
          width: 100%;
          margin-bottom: 18px;
        }}

        .nav a {{
          display: flex;
          align-items: center;
          justify-content: center;
          text-decoration: none;
          color: var(--text);
          background: #fff;
          border: 1px solid var(--line);
          border-radius: 12px;
          padding: 10px 14px;
          font-size: 14px;
          font-weight: 600;
          box-shadow: 0 2px 8px rgba(0,0,0,.03);
          min-height: 44px;
          text-align: center;
        }}

        .nav a.active {{
          background: var(--blue);
          color: #fff;
          border-color: var(--blue);
          box-shadow: 0 2px 10px rgba(37, 99, 235, .18);
        }}

        .nav a.active:hover {{
          color: #fff;
          border-color: var(--blue);
        }}

        .nav a:hover {{
          border-color: var(--blue);
          color: var(--blue);
        }}

        .layout {{
          display: grid;
          grid-template-columns: 1fr;
          gap: 18px;
        }}

        .card {{
          background: var(--card);
          border-radius: var(--radius);
          padding: 22px;
          box-shadow: var(--shadow);
        }}

        .page-title {{
          margin: 0 0 16px;
          font-size: 30px;
          font-weight: 800;
        }}

        .muted {{
          color: var(--muted);
        }}

        .stats {{
          display: grid;
          grid-template-columns: repeat(4, minmax(180px, 1fr));
          gap: 14px;
          margin-bottom: 18px;
        }}

        .stat {{
          background: #fff;
          border: 1px solid var(--line);
          border-radius: 14px;
          padding: 16px;
        }}

        .stat-label {{
          font-size: 13px;
          color: var(--muted);
          margin-bottom: 8px;
        }}

        .stat-value {{
          font-size: 28px;
          font-weight: 800;
        }}

        table {{
          width: 100%;
          min-width: 1200px;
          border-collapse: collapse;
          background: #fff;
          border-radius: 12px;
          overflow: hidden;
        }}

        th {{
          text-align: left;
          background: #f8fafc;
          color: #334155;
          font-size: 13px;
          font-weight: 700;
          padding: 12px 10px;
          border-bottom: 1px solid var(--line);
          position: sticky;
          top: 0;
        }}

        td {{
          padding: 11px 10px;
          border-bottom: 1px solid #eef2f7;
          font-size: 14px;
          vertical-align: top;
        }}

        tr:hover td {{
          background: #fafcff;
        }}

        .table-wrap {{
          overflow: auto;
          border: 1px solid var(--line);
          border-radius: 14px;
          width: 100%;
        }}

        .toolbar {{
          display: flex;
          flex-wrap: wrap;
          gap: 10px;
          align-items: center;
          margin-bottom: 16px;
        }}

        .toolbar input[type=text],
        .toolbar input[type=date] {{
          height: 42px;
          padding: 0 12px;
          border: 1px solid #cbd5e1;
          border-radius: 10px;
          background: #fff;
          min-width: 220px;
        }}

    .toolbar select {{
      height: 42px;
      padding: 0 12px;
      border: 1px solid #cbd5e1;
      border-radius: 10px;
      background: #fff;
      min-width: 180px;
    }}

        .btn {{
          display: inline-flex;
          align-items: center;
          justify-content: center;
          height: 42px;
          padding: 0 14px;
          border-radius: 10px;
          border: 1px solid var(--line);
          background: #fff;
          color: var(--text);
          text-decoration: none;
          font-weight: 600;
          cursor: pointer;
        }}

        .btn.primary {{
          background: var(--blue);
          color: #fff;
          border-color: var(--blue);
        }}

        .btn:hover {{
          opacity: .95;
        }}

        .badge {{
          display: inline-block;
          padding: 6px 10px;
          border-radius: 999px;
          font-size: 12px;
          font-weight: 700;
          white-space: nowrap;
        }}

        .badge.active {{ background: var(--green-soft); color: var(--green); }}
        .badge.pending {{ background: var(--yellow-soft); color: var(--yellow); }}
        .badge.expired, .badge.closed {{ background: #e5e7eb; color: #475569; }}
        .badge.blocked, .badge.error {{ background: var(--red-soft); color: var(--red); }}

        ul.quick-links {{
          margin: 0;
          padding-left: 18px;
        }}

        ul.quick-links li {{
          margin: 8px 0;
        }}

        @media (max-width: 980px) {{
          .stats {{
            grid-template-columns: repeat(2, minmax(160px, 1fr));
          }}
        }}

        @media (max-width: 640px) {{
          .wrap {{
            padding: 14px;
          }}
          .stats {{
            grid-template-columns: 1fr;
          }}
          .page-title {{
            font-size: 24px;
          }}
        }}
      </style>
    </head>
    <body>
      <div class="wrap">
        <div class="topbar">
          <div>
            <div class="brand">Miracleon Captive Portal</div>
            <div class="subtitle">Управление гостевым Wi-Fi, авторизацией и выгрузками</div>
          </div>
        </div>

        <div class="nav">
            {nav_html}
        </div>

        <div class="layout">
          <div class="card">
            <h1 class="page-title">{escape(title)}</h1>
            {body}
          </div>
        </div>
      </div>
    </body>
    </html>
    """
    return HTMLResponse(html)




