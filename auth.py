import re
from datetime import datetime, timezone

from db import db


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
    return (mac or "").strip().upper()


def get_active_guest(phone: str):
    conn = db()
    row = conn.execute("""
        SELECT *
        FROM guests
        WHERE phone = ?
          AND status = 'active'
          AND datetime(last_auth_at) >= datetime('now', '-3 days')
        LIMIT 1
    """, (phone,)).fetchone()
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
        SELECT * FROM guest_sessions
        WHERE phone = ? AND mac = ? AND status = 'active'
        ORDER BY id DESC
        LIMIT 1
    """, (phone, mac)).fetchone()
    conn.close()
    return row


def active_sessions_count(phone: str):
    conn = db()
    cnt = conn.execute("""
        SELECT COUNT(*) AS cnt
        FROM guest_sessions
        WHERE phone = ? AND status = 'active'
    """, (phone,)).fetchone()["cnt"]
    conn.close()
    return cnt


def start_session(guest_id, phone, mac, ip, nas_id, hotel, ssid, vlan_id):
    conn = db()
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
        now_iso(),
        now_iso(),
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
            last_seen_at = ?
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


