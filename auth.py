import re
from datetime import datetime, timezone

from app_services.settings_store import get_setting
from db import db

MIN_REAUTH_DAYS = 1
DEFAULT_REAUTH_DAYS = 4
MAX_REAUTH_DAYS = 30


def now():
    return datetime.now(timezone.utc)


def now_iso():
    return now().isoformat()


def normalize_phone(phone: str) -> str:
    raw = (phone or "").strip()
    digits = re.sub(r"\D", "", raw)

    if len(digits) == 10:
        return "7" + digits

    if len(digits) == 11 and digits.startswith("8"):
        return "7" + digits[1:]

    if len(digits) == 11 and digits.startswith("7"):
        return digits

    raise ValueError("invalid phone")


def normalize_mac(mac: str) -> str:
    raw = (mac or "").strip()
    hex_only = re.sub(r"[^0-9A-Fa-f]", "", raw)

    if len(hex_only) == 12:
        h = hex_only.upper()
        return ":".join(h[i:i+2] for i in range(0, 12, 2))

    return raw.upper()


def make_room_identity(room_num: str, surname: str) -> str:
    room = (room_num or "").strip()
    sur = re.sub(r"\s+", " ", (surname or "").strip().lower())
    return f"room:{room}|{sur}"


def get_reauth_window_days() -> int:
    try:
        days = int(get_setting("auth.reauth_days", DEFAULT_REAUTH_DAYS))
    except Exception:
        days = DEFAULT_REAUTH_DAYS

    return max(MIN_REAUTH_DAYS, min(MAX_REAUTH_DAYS, days))


def reauth_window_modifier() -> str:
    return f"-{get_reauth_window_days()} days"


def get_active_guest(phone: str):
    modifier = reauth_window_modifier()
    conn = db()
    row = conn.execute("""
        SELECT *
        FROM guests
        WHERE phone = ?
          AND status = 'active'
          AND datetime(last_auth_at) >= datetime('now', ?)
        LIMIT 1
    """, (phone, modifier)).fetchone()
    conn.close()
    return row


def get_or_create_guest(phone: str, hotel: str | None):
    conn = db()
    row = conn.execute("SELECT * FROM guests WHERE phone = ?", (phone,)).fetchone()

    if row:
        conn.execute("""
            UPDATE guests
            SET updated_at = ?, last_auth_at = ?
            WHERE id = ?
        """, (now_iso(), now_iso(), row["id"]))
        conn.commit()
        row = conn.execute("SELECT * FROM guests WHERE phone = ?", (phone,)).fetchone()
        conn.close()
        return row

    created = now_iso()
    conn.execute("""
        INSERT INTO guests (phone, first_verified_at, first_hotel, auth_method, status, created_at, updated_at, last_auth_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (phone, created, hotel, "call", "active", created, created, created))
    conn.commit()
    row = conn.execute("SELECT * FROM guests WHERE phone = ?", (phone,)).fetchone()
    conn.close()
    return row


def get_or_create_room_guest(room_num: str, surname: str, hotel: str | None):
    identity = make_room_identity(room_num, surname)

    room_num = str(room_num or "").strip()
    surname = str(surname or "").strip()

    conn = db()
    row = conn.execute("SELECT * FROM guests WHERE phone = ?", (identity,)).fetchone()

    if row:
        conn.execute("""
            UPDATE guests
            SET updated_at = ?,
                last_auth_at = ?,
                auth_method = 'room',
                auth_type = 'room',
                room_number = ?,
                guest_name = ?
            WHERE id = ?
        """, (now_iso(), now_iso(), room_num, surname, row["id"]))

        conn.commit()
        row = conn.execute("SELECT * FROM guests WHERE phone = ?", (identity,)).fetchone()
        conn.close()
        return row

    created = now_iso()
    conn.execute("""
        INSERT INTO guests (
            phone,
            first_verified_at,
            first_hotel,
            auth_method,
            auth_type,
            room_number,
            guest_name,
            status,
            created_at,
            updated_at,
            last_auth_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        identity,
        created,
        hotel,
        "room",
        "room",
        room_num,
        surname,
        "active",
        created,
        created,
        created,
    ))

    conn.commit()
    row = conn.execute("SELECT * FROM guests WHERE phone = ?", (identity,)).fetchone()
    conn.close()
    return row


def touch_guest_auth(phone: str):
    conn = db()
    conn.execute("""
        UPDATE guests
        SET updated_at = ?, last_auth_at = ?
        WHERE phone = ?
    """, (now_iso(), now_iso(), phone))
    conn.commit()
    conn.close()


def get_live_pending(phone: str, mac: str):
    conn = db()
    row = conn.execute("""
        SELECT *
        FROM pending_auth
        WHERE phone = ?
          AND mac = ?
          AND status = 'pending'
        ORDER BY id DESC
        LIMIT 1
    """, (phone, mac)).fetchone()
    conn.close()
    return row


def get_open_session(phone: str, mac: str):
    conn = db()
    row = conn.execute("""
        SELECT *
        FROM guest_sessions
        WHERE mac = ?
          AND (
                status = 'active'
                OR (
                    ended_at IS NOT NULL
                    AND datetime(ended_at) >= datetime('now', '-60 minutes')
                )
          )
        ORDER BY
          CASE WHEN status = 'active' THEN 0 ELSE 1 END,
          CASE WHEN phone = ? THEN 0 ELSE 1 END,
          datetime(COALESCE(last_seen_at, started_at, ended_at)) DESC,
          id DESC
        LIMIT 1
    """, (mac, phone)).fetchone()
    conn.close()
    return row


def active_sessions_count(phone: str):
    conn = db()
    cnt = conn.execute("""
        SELECT COUNT(DISTINCT mac) AS cnt
        FROM guest_sessions
        WHERE phone = ?
          AND status = 'active'
          AND mac IS NOT NULL
          AND mac != ''
    """, (phone,)).fetchone()["cnt"]
    conn.close()
    return cnt


def start_session(guest_id, phone, mac, ip, nas_id, hotel, ssid, vlan_id):
    conn = db()
    ts = now_iso()

    conn.execute("""
        UPDATE guest_sessions
        SET status = 'closed',
            ended_at = COALESCE(last_seen_at, started_at, ?),
            terminate_cause = 'duplicate_active_cleanup'
        WHERE mac = ?
          AND status = 'active'
          AND ended_at IS NULL
    """, (ts, mac))

    conn.execute("""
        INSERT INTO guest_sessions
        (guest_id, phone, mac, ip, nas_id, hotel, ssid, vlan_id, started_at, last_seen_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        guest_id,
        phone,
        mac,
        ip,
        nas_id,
        hotel,
        ssid,
        vlan_id,
        ts,
        ts,
        "active",
    ))
    conn.commit()
    conn.close()


def update_session(session_id, ip, nas_id, hotel, ssid, vlan_id):
    conn = db()
    conn.execute("""
        UPDATE guest_sessions
        SET ip = ?,
            nas_id = ?,
            hotel = ?,
            ssid = ?,
            vlan_id = ?,
            last_seen_at = ?,
            status = 'active',
            ended_at = NULL,
            terminate_cause = NULL
        WHERE id = ?
    """, (
        ip,
        nas_id,
        hotel,
        ssid,
        vlan_id,
        now_iso(),
        session_id
    ))
    conn.commit()
    conn.close()
    

def get_recent_authorized_session_by_mac(mac: str):
    modifier = reauth_window_modifier()
    conn = db()
    row = conn.execute("""
        SELECT
            s.*,
            g.id AS guest_id,
            g.phone AS guest_phone,
            g.status AS guest_status,
            g.last_auth_at AS guest_last_auth_at
        FROM guest_sessions s
        JOIN guests g ON g.id = s.guest_id
        WHERE s.mac = ?
          AND g.status = 'active'
          AND datetime(COALESCE(s.last_seen_at, s.ended_at, s.started_at, g.last_auth_at)) >= datetime('now', ?)
        ORDER BY datetime(COALESCE(s.last_seen_at, s.ended_at, s.started_at, g.last_auth_at)) DESC, s.id DESC
        LIMIT 1
    """, (mac, modifier)).fetchone()
    conn.close()
    return row
