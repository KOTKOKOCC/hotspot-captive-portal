import os
import secrets
import hashlib
from datetime import datetime, timedelta, timezone
from cryptography.fernet import Fernet

from db import db
from auth import now_iso, now, get_or_create_guest, get_open_session, start_session, update_session
from services import accept_reply, audit


def get_voucher_fernet():
    key = os.getenv("VOUCHER_SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError("VOUCHER_SECRET_KEY is not set")
    return Fernet(key.encode())


def encrypt_value(value: str | None) -> str:
    if not value:
        return ""
    return get_voucher_fernet().encrypt(value.encode()).decode()


def hash_code(code: str) -> str:
    normalized = normalize_voucher_code(code)
    return hashlib.sha256(normalized.encode()).hexdigest()


def normalize_voucher_code(code: str) -> str:
    return "".join((code or "").upper().replace("-", "").split())


def format_voucher_code(code: str) -> str:
    code = normalize_voucher_code(code)
    return "-".join([code[i:i+4] for i in range(0, len(code), 4)])


def generate_voucher_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(12))
    return format_voucher_code(raw)


def ensure_voucher_tables():
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vouchers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code_hash TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'active',
            max_devices INTEGER NOT NULL DEFAULT 1,
            valid_from TEXT,
            valid_until TEXT,
            site TEXT,
            room_num TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL,
            revoked_at TEXT,
            revoke_reason TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS voucher_guests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            voucher_id INTEGER NOT NULL,
            full_name_enc TEXT NOT NULL,
            passport_enc TEXT NOT NULL,
            birth_date_enc TEXT,
            phone_enc TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(voucher_id) REFERENCES vouchers(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS voucher_devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            voucher_id INTEGER NOT NULL,
            mac TEXT NOT NULL,
            ip TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            UNIQUE(voucher_id, mac)
        )
    """)

    cols = [r["name"] for r in conn.execute("PRAGMA table_info(vouchers)").fetchall()]

    if "code_enc" not in cols:
        conn.execute("ALTER TABLE vouchers ADD COLUMN code_enc TEXT")    

    conn.commit()
    conn.close()

    

def create_voucher(
    full_name: str,
    passport: str,
    birth_date: str = "",
    phone: str = "",
    site: str = "",
    room_num: str = "",
    max_devices: int = 1,
    valid_days: int = 1,
    created_by: str = "reception",
):
    code = generate_voucher_code()
    code_hash = hash_code(code)

    valid_from = now_iso()
    valid_until = (now() + timedelta(days=max(1, int(valid_days)))).isoformat()

    conn = db()
    cur = conn.execute("""
        INSERT INTO vouchers
        (code_hash, code_enc, status, max_devices, valid_from, valid_until, site, room_num, created_by, created_at)
        VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)
    """, (
        code_hash,
        encrypt_value(code),
        max(1, int(max_devices)),
        valid_from,
        valid_until,
        site,
        room_num,
        created_by,
        now_iso(),
    ))

    voucher_id = cur.lastrowid

    conn.execute("""
        INSERT INTO voucher_guests
        (voucher_id, full_name_enc, passport_enc, birth_date_enc, phone_enc, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        voucher_id,
        encrypt_value(full_name),
        encrypt_value(passport),
        encrypt_value(birth_date),
        encrypt_value(phone),
        now_iso(),
    ))

    conn.commit()
    conn.close()

    return {
        "id": voucher_id,
        "code": code,
        "valid_until": valid_until,
        "max_devices": max_devices,
    }


def decrypt_value(value: str | None) -> str:
    if not value:
        return ""
    return get_voucher_fernet().decrypt(value.encode()).decode()


def decrypt_voucher_code(value: str | None) -> str:
    try:
        return decrypt_value(value)
    except Exception:
        return "—"


def session_timeout_from_voucher(voucher) -> int:
    if not voucher["valid_until"]:
        return 86400

    exp = datetime.fromisoformat(voucher["valid_until"])
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)

    seconds = int((exp - now()).total_seconds())
    return max(60, seconds)


def verify_voucher_radius(code: str, mac: str, ip: str | None, nas_id: str | None, hotel: str | None, ssid: str | None, vlan_id):
    normalized = normalize_voucher_code(code)
    if not normalized:
        return None, "empty_voucher"

    conn = db()
    voucher = conn.execute("""
        SELECT *
        FROM vouchers
        WHERE code_hash = ?
        LIMIT 1
    """, (hash_code(normalized),)).fetchone()

    if not voucher:
        conn.close()
        return None, "voucher_not_found"

    if voucher["status"] != "active":
        conn.close()
        return None, "voucher_inactive"

    if voucher["valid_until"]:
        exp = datetime.fromisoformat(voucher["valid_until"])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)

        if now() > exp:
            conn.execute("UPDATE vouchers SET status='expired' WHERE id=?", (voucher["id"],))
            conn.commit()
            conn.close()
            return None, "voucher_expired"

    device = conn.execute("""
        SELECT *
        FROM voucher_devices
        WHERE voucher_id = ? AND mac = ?
        LIMIT 1
    """, (voucher["id"], mac)).fetchone()

    if not device:
        used_count = conn.execute("""
            SELECT COUNT(*) AS cnt
            FROM voucher_devices
            WHERE voucher_id = ?
        """, (voucher["id"],)).fetchone()["cnt"]

        
        if used_count >= int(voucher["max_devices"]):
            audit(
                "radius_reject_voucher_device_limit",
                phone="voucher:" + normalized,
                mac=mac,
                ip=ip,
                nas_id=nas_id,
                hotel=hotel,
                ssid=ssid,
                vlan_id=vlan_id,
                details=f"voucher_id={voucher['id']}, max_devices={voucher['max_devices']}"
            )
            conn.close()
            return None, "voucher_device_limit"


        conn.execute("""
            INSERT INTO voucher_devices
            (voucher_id, mac, ip, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
        """, (voucher["id"], mac, ip, now_iso(), now_iso()))
    else:
        conn.execute("""
            UPDATE voucher_devices
            SET ip = ?, last_seen_at = ?
            WHERE id = ?
        """, (ip, now_iso(), device["id"]))

    conn.commit()
    conn.close()

    identity = "voucher:" + normalized
    guest = get_or_create_guest(identity, hotel)

    session = get_open_session(identity, mac)
    if session:
        update_session(session["id"], ip, nas_id, hotel, ssid, vlan_id)
    else:
        start_session(guest["id"], identity, mac, ip, nas_id, hotel, ssid, vlan_id)

    conn2 = db()
    conn2.execute("""
        UPDATE guest_sessions
        SET auth_method = 'voucher'
        WHERE phone = ? AND mac = ? AND status = 'active'
    """, (identity, mac))
    conn2.commit()
    conn2.close()

    audit(
        "radius_accept_voucher",
        phone=identity,
        mac=mac,
        ip=ip,
        nas_id=nas_id,
        hotel=hotel,
        ssid=ssid,
        vlan_id=vlan_id,
        details=f"voucher_id={voucher['id']}"
    )

    reply = accept_reply()
    reply["Session-Timeout"] = session_timeout_from_voucher(voucher)

    return reply, "ok"