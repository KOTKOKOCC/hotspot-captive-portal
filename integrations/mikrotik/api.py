import time
import threading
import routeros_api
import logging

from db import db
from app_services.settings_store import get_setting

logger = logging.getLogger(__name__)


def get_mt_config():
    return {
        "host": get_setting("mikrotik.host", ""),
        "port": int(get_setting("mikrotik.port", "8728")),
        "user": get_setting("mikrotik.user", ""),
        "password": get_setting("mikrotik.password", ""),
        "device_sync_interval": int(get_setting("mikrotik.device_sync_interval", "300")),
    }


def mt_api():
    cfg = get_mt_config()

    connection = routeros_api.RouterOsApiPool(
        cfg["host"],
        username=cfg["user"],
        password=cfg["password"],
        port=cfg["port"],
        plaintext_login=True
    )
    return connection


def fetch_dhcp_leases():
    pool = None
    try:
        pool = mt_api()
        api = pool.get_api()
        lease_res = api.get_resource("/ip/dhcp-server/lease")
        rows = lease_res.get()
        result = []

        for row in rows:
            result.append({
                "address": row.get("address", ""),
                "mac": (row.get("mac-address", "") or "").upper(),
                "host_name": row.get("host-name", "") or "",
                "class_id": row.get("class-id", "") or "",
                "status": row.get("status", "") or "",
            })

        return result
    finally:
        if pool:
            pool.disconnect()


def sync_session_device_names():
    leases = fetch_dhcp_leases()

    by_ip = {}
    by_mac = {}

    for lease in leases:
        ip = lease["address"]
        mac = lease["mac"]
        host_name = lease["host_name"]

        if host_name:
            if ip:
                by_ip[ip] = host_name
            if mac:
                by_mac[mac] = host_name

    conn = db()
    rows = conn.execute("""
        SELECT id, ip, mac
        FROM guest_sessions
        WHERE status = 'active'
           OR started_at >= datetime('now', '-1 day')
        ORDER BY started_at DESC
        LIMIT 1000
    """).fetchall()

    updated = 0

    for row in rows:
        ip = row["ip"] or ""
        mac = (row["mac"] or "").upper()

        device_name = by_ip.get(ip) or by_mac.get(mac)
        if not device_name:
            continue

        conn.execute("""
            UPDATE guest_sessions
            SET device_name = ?
            WHERE id = ?
        """, (device_name, row["id"]))
        updated += 1

    conn.commit()
    conn.close()
    return updated


def device_name_sync_worker():
    while True:
        try:
            updated = sync_session_device_names()
            logger.info("mikrotik sync updated: %s", updated)
        except Exception as e:
            logger.warning("mikrotik sync waiting for MikroTik: %s", e)

        time.sleep(get_mt_config()["device_sync_interval"])
        

def start_device_name_sync_worker():
    t = threading.Thread(target=device_name_sync_worker, daemon=True)
    t.start()
    return t


def disconnect_hotspot_active_by_mac(mac: str) -> int:
    mac = (mac or "").upper().strip()
    if not mac:
        return 0

    pool = None
    removed = 0

    try:
        pool = mt_api()
        api = pool.get_api()
        active_res = api.get_resource("/ip/hotspot/active")

        rows = active_res.get()
        for row in rows:
            row_mac = (row.get("mac-address", "") or "").upper().strip()
            if row_mac == mac:
                active_res.remove(id=row["id"])
                removed += 1

        return removed
    finally:
        if pool:
            pool.disconnect()


def fetch_hotspot_active():
    pool = None
    try:
        pool = mt_api()
        api = pool.get_api()

        active_res = api.get_resource("/ip/hotspot/active")
        rows = active_res.get()

        result = []
        for row in rows:
            result.append({
                "server": row.get("server", ""),
                "user": row.get("user", ""),
                "address": row.get("address", ""),
                "mac": (row.get("mac-address", "") or "").upper(),
                "uptime": row.get("uptime", ""),
                "idle_time": row.get("idle-time", ""),
            })

        return result
    finally:
        if pool:
            pool.disconnect()