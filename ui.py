from datetime import datetime, timezone
from html import escape
from urllib.parse import quote_plus

from fastapi.responses import HTMLResponse

from config import APP_VERSION
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

        if val == "pending" and row is not None:
            expires_at = row["expires_at"] if "expires_at" in row.keys() else None
            if expires_at:
                try:
                    exp = datetime.fromisoformat(str(expires_at))
                    if exp.tzinfo is None:
                        exp = exp.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) > exp:
                        val = "expired"
                        raw = "expired"
                except Exception:
                   pass

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
            try:
                value = row[col]
            except (KeyError, IndexError):
                value = ""

            if value is None:
                value = ""

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
                if "guest_id" in row.keys() and row["guest_id"]:
                    phone_link = f'/admin/client?guest_id={quote_plus(str(row["guest_id"]))}'
                else:
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



def admin_page(title: str, body: str, active_tab: str = "", role: str = "admin") -> HTMLResponse:
    def nav_item(href: str, label: str, key: str) -> str:
        cls = "active" if active_tab == key else ""
        return f'<a href="{href}" class="{cls}">{label}</a>'

    if role == "reception":
        nav_html = "".join([
            nav_item("/admin/vouchers", "Ваучеры", "vouchers"),
            nav_item("/admin/logout", "Выход", "logout"),
        ])

    elif role == "it":
        nav_html = "".join([
            nav_item("/admin", "Главная", "home"),
            nav_item("/admin/guests", "Гости", "guests"),
            nav_item("/admin/sessions", "Сессии", "sessions"),
            nav_item("/admin/pending", "Ожидание", "pending"),
            nav_item("/admin/calls", "Звонки", "calls"),
            nav_item("/admin/vouchers", "Ваучеры", "vouchers"),
            nav_item("/admin/find", "Поиск", "find"),
            nav_item("/admin/logout", "Выход", "logout"),
        ])
    
    else:
        nav_html = "".join([
            nav_item("/admin", "Главная", "home"),
            nav_item("/admin/guests", "Гости", "guests"),
            nav_item("/admin/sessions", "Сессии", "sessions"),
            nav_item("/admin/pending", "Ожидание", "pending"),
            nav_item("/admin/calls", "Звонки", "calls"),
            nav_item("/admin/audit", "Аудит", "audit"),
            nav_item("/admin/vouchers", "Ваучеры", "vouchers"),
            nav_item("/admin/find", "Поиск", "find"),
            nav_item("/admin/system", "Система", "system"),
            nav_item("/admin/logout", "Выход", "logout"),
        ])
    
    html = f"""
    <!doctype html>
    <html lang="ru">
    <head>
      <meta charset="utf-8">
      <title>{escape(title)}</title>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <link rel="stylesheet" href="/static/admin.css">
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
      <div class="watermark" aria-hidden="true">
        <div>Created by S.Z.</div>
        <div class="watermark-version">v{escape(APP_VERSION)}</div>
      </div>
    </body>
    </html>
    """
    return HTMLResponse(html)



