from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import ipaddress
import threading
import time
import json

from db import db
from auth import now, now_iso, normalize_phone, normalize_mac

DISPLAY_TZ = ZoneInfo("Europe/Moscow")



def normalize_accounting_event_time(value: str | None) -> str:
    if not value:
        return now_iso()

    value = str(value).strip()

    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=DISPLAY_TZ)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        pass

    for fmt in ("%b %d %Y %H:%M:%S %Z",):
        try:
            dt = datetime.strptime(value, fmt)
            dt = dt.replace(tzinfo=DISPLAY_TZ)
            return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            continue

    return now_iso()


def resolve_network_info(ip: str | None):
    empty = {
        "hotel_name": None,
        "ssid_name": None,
        "vlan_id": None,
        "mikrotik_interface": None,
        "hotspot_server": None,
    }

    if not ip:
        return empty

    try:
        client_ip = ipaddress.ip_address(ip)
    except ValueError:
        return empty

    conn = db()
    rows = conn.execute("SELECT * FROM network_map WHERE is_active = 1").fetchall()
    conn.close()

    for row in rows:
        try:
            net = ipaddress.ip_network(row["subnet_cidr"], strict=False)
        except ValueError:
            continue

        if client_ip in net:
            return {
                "hotel_name": row["hotel_name"],
                "ssid_name": row["ssid_name"],
                "vlan_id": row["vlan_id"],
                "mikrotik_interface": row["mikrotik_interface"],
                "hotspot_server": row["hotspot_server"],
            }

    return empty


def audit(event_type: str, phone=None, mac=None, ip=None, nas_id=None, hotel=None, ssid=None, vlan_id=None, details=None):
    conn = db()
    conn.execute("""
        INSERT INTO audit_log (phone, mac, ip, nas_id, hotel, ssid, vlan_id, event_type, event_time, details)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (phone, mac, ip, nas_id, hotel, ssid, vlan_id, event_type, now_iso(), details))
    conn.commit()
    conn.close()


def save_radius_accounting(evt):
    conn = db()

    event_time = normalize_accounting_event_time(evt.event_time)
    mac = normalize_mac(evt.mac) if evt.mac else None

    conn.execute("""
        INSERT INTO radius_accounting
        (
            acct_session_id,
            username,
            mac,
            ip,
            nas_ip,
            nas_id,
            nas_port_id,
            called_station_id,
            acct_status_type,
            terminate_cause,
            session_time,
            event_time,
            raw_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        evt.acct_session_id,
        evt.username,
        mac,
        evt.ip,
        evt.nas_ip,
        evt.nas_id,
        evt.nas_port_id,
        evt.called_station_id,
        evt.acct_status_type,
        evt.terminate_cause,
        evt.session_time,
        event_time,
        json.dumps(evt.model_dump(), ensure_ascii=False),
        now_iso()
    ))

    phone = None
    try:
        if evt.username:
            phone = normalize_phone(evt.username)
    except Exception:
        phone = evt.username

    if evt.acct_status_type == "Start":
        row = None

        if evt.acct_session_id:
            row = conn.execute("""
                SELECT id FROM guest_sessions
                WHERE acct_session_id = ?
                ORDER BY id DESC
                LIMIT 1
            """, (evt.acct_session_id,)).fetchone()

        if not row and phone and mac:
            row = conn.execute("""
                SELECT id
                FROM guest_sessions
                WHERE status = 'active'
                  AND ended_at IS NULL
                  AND acct_session_id IS NULL
                  AND phone = ?
                  AND mac = ?
                  AND started_at >= datetime('now', '-10 minutes')
                ORDER BY id DESC
                LIMIT 1
            """, (phone, mac)).fetchone()

        if not row and mac:
            row = conn.execute("""
                SELECT id
                FROM guest_sessions
                WHERE status = 'active'
                  AND ended_at IS NULL
                  AND acct_session_id IS NULL
                  AND mac = ?
                  AND started_at >= datetime('now', '-10 minutes')
                ORDER BY id DESC
                LIMIT 1
            """, (mac,)).fetchone()

        if row:
            conn.execute("""
                UPDATE guest_sessions
                SET phone = COALESCE(?, phone),
                    mac = COALESCE(?, mac),
                    ip = COALESCE(?, ip),
                    nas_id = COALESCE(?, nas_id),
                    acct_session_id = COALESCE(?, acct_session_id),
                    last_seen_at = ?,
                    status = 'active',
                    acct_session_time = COALESCE(?, acct_session_time)
                WHERE id = ?
            """, (
                phone,
                mac,
                evt.ip,
                evt.nas_id,
                evt.acct_session_id,
                event_time,
                evt.session_time,
                row["id"]
            ))
        else:
            netinfo = resolve_network_info(evt.ip)
            hotel = netinfo["hotel_name"]
            ssid = netinfo["ssid_name"]
            vlan_id = netinfo["vlan_id"]

            guest_id = None
            if phone:
                guest = conn.execute("""
                    SELECT id FROM guests
                    WHERE phone = ?
                    ORDER BY id DESC
                    LIMIT 1
                """, (phone,)).fetchone()
                if guest:
                    guest_id = guest["id"]

            conn.execute("""
                INSERT INTO guest_sessions
                (
                    guest_id, phone, mac, ip, nas_id, hotel, ssid, vlan_id,
                    started_at, ended_at, expires_at, status,
                    acct_session_id, last_seen_at, terminate_cause, acct_session_time
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                guest_id,
                phone,
                mac,
                evt.ip,
                evt.nas_id,
                hotel,
                ssid,
                vlan_id,
                event_time,
                None,
                None,
                "active",
                evt.acct_session_id,
                event_time,
                None,
                evt.session_time or 0
            ))

    elif evt.acct_status_type == "Interim-Update":
        if evt.acct_session_id:
            conn.execute("""
                UPDATE guest_sessions
                SET last_seen_at = ?,
                    ip = COALESCE(?, ip),
                    nas_id = COALESCE(?, nas_id),
                    acct_session_time = COALESCE(?, acct_session_time)
                WHERE acct_session_id = ?
            """, (
                event_time,
                evt.ip,
                evt.nas_id,
                evt.session_time,
                evt.acct_session_id
            ))

    elif evt.acct_status_type == "Stop":
        updated = 0
        if evt.acct_session_id:
            cur = conn.execute("""
                UPDATE guest_sessions
                SET ended_at = ?,
                    last_seen_at = ?,
                    status = 'closed',
                    terminate_cause = ?,
                    acct_session_time = COALESCE(?, acct_session_time)
                WHERE acct_session_id = ? AND status = 'active'
            """, (
                event_time,
                event_time,
                evt.terminate_cause,
                evt.session_time,
                evt.acct_session_id
            ))
            updated = cur.rowcount

        if updated == 0 and mac:
            conn.execute("""
                UPDATE guest_sessions
                SET ended_at = ?,
                    last_seen_at = ?,
                    status = 'closed',
                    terminate_cause = ?,
                    acct_session_time = COALESCE(?, acct_session_time)
                WHERE mac = ? AND status = 'active'
            """, (
                event_time,
                event_time,
                evt.terminate_cause,
                evt.session_time,
                mac
            ))

    conn.commit()
    conn.close()


_cleanup_worker_started = False
_cleanup_worker_lock = threading.Lock()


def _cleanup_worker_loop(interval_seconds: int = 60):
    while True:
        try:
            run_cleanup()
        except Exception as e:
            print(f"[cleanup] worker iteration failed: {e}")
        time.sleep(interval_seconds)


def start_cleanup_worker(interval_seconds: int = 60):
    global _cleanup_worker_started
    with _cleanup_worker_lock:
        if _cleanup_worker_started:
            return
        thread = threading.Thread(
            target=_cleanup_worker_loop,
            args=(interval_seconds,),
            daemon=True,
            name="cleanup-worker",
        )
        thread.start()
        _cleanup_worker_started = True


def cleanup_db():
    conn = db()
    current = now()

    rows = conn.execute("""
        SELECT id, status, expires_at
        FROM pending_auth
        WHERE status IN ('pending', 'expired', 'verified')
    """).fetchall()

    to_delete = []
    to_expire = []

    for row in rows:
        try:
            exp = datetime.fromisoformat(row["expires_at"])
        except Exception:
            continue

        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)

        if row["status"] == "pending" and current > exp:
            to_expire.append(row["id"])

        elif row["status"] == "expired" and current > exp + timedelta(days=1):
            to_delete.append(row["id"])

        elif row["status"] == "verified" and current > exp + timedelta(days=3):
            to_delete.append(row["id"])

    expired = 0
    deleted = 0

    if to_expire:
        conn.executemany(
            "UPDATE pending_auth SET status='expired' WHERE id = ?",
            [(x,) for x in to_expire]
        )
        conn.commit()
        expired = len(to_expire)

    if to_delete:
        conn.executemany(
            "DELETE FROM pending_auth WHERE id = ?",
            [(x,) for x in to_delete]
        )
        conn.commit()
        deleted = len(to_delete)

    conn.close()
    return {"expired": expired, "deleted": deleted}


def cleanup_stale_sessions():
    conn = db()
    current = now()
    closed = 0

    rows = conn.execute("""
        SELECT id, started_at
        FROM guest_sessions
        WHERE status = 'active'
          AND ended_at IS NULL
          AND acct_session_id IS NULL
    """).fetchall()

    to_close = []

    for row in rows:
        try:
            started = datetime.fromisoformat(row["started_at"])
        except Exception:
            continue

        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)

        if current - started > timedelta(minutes=30):
            to_close.append(row["id"])

    if to_close:
        conn.executemany("""
            UPDATE guest_sessions
            SET ended_at = ?, status = 'closed', terminate_cause = 'Legacy-Cleanup'
            WHERE id = ?
        """, [(now_iso(), x) for x in to_close])
        conn.commit()
        closed = len(to_close)

    conn.close()
    return closed


def run_cleanup():
    pending_stats = cleanup_db()
    stale_sessions_closed = cleanup_stale_sessions()
    return {
        "pending_expired": pending_stats["expired"],
        "pending_deleted": pending_stats["deleted"],
        "stale_sessions_closed": stale_sessions_closed,
    }


def get_guest_last_activity(phone: str) -> str | None:
    conn = db()
    row = conn.execute("""
        SELECT COALESCE(
            MAX(last_seen_at),
            MAX(ended_at),
            MAX(started_at)
        ) AS last_activity
        FROM guest_sessions
        WHERE phone = ?
    """, (phone,)).fetchone()
    conn.close()
    return row["last_activity"] if row else None


def accept_reply():
    return {
        "control:Cleartext-Password": "callauth",
        "Mikrotik-Group": "guest_default",
        "Session-Timeout": "259200"
    }














