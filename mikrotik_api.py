import time
import threading
import routeros_api

from db import db
from config import MT_HOST, MT_PORT, MT_USER, MT_PASS, DEVICE_SYNC_INTERVAL





def mt_api():
    connection = routeros_api.RouterOsApiPool(
        MT_HOST,
        username=MT_USER,
        password=MT_PASS,
        port=MT_PORT,
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
            print(f"[mikrotik-sync] updated: {updated}")
        except Exception as e:
            print(f"[mikrotik-sync] waiting for MikroTik: {e}")

        time.sleep(DEVICE_SYNC_INTERVAL)


def start_device_name_sync_worker():
    t = threading.Thread(target=device_name_sync_worker, daemon=True)
    t.start()
    return t