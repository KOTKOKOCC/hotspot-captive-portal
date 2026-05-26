from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import ipaddress
import json
import sqlite3
import time
import threading
import logging

from db import db
from auth import now, now_iso, normalize_phone, normalize_mac
from config import BASE_DIR, DB_PATH
from app_services.export_jobs import cleanup_export_job_files
from app_services.settings_store import get_setting, set_setting

DISPLAY_TZ = ZoneInfo("Europe/Moscow")

logger = logging.getLogger(__name__)

MIN_RETENTION_DAYS = 180
DEFAULT_RETENTION_DAYS = 180
DEFAULT_RETENTION_BATCH_SIZE = 5000
DEFAULT_RETENTION_INTERVAL_HOURS = 24
DEFAULT_EXPORT_FILE_RETENTION_DAYS = 7
DEFAULT_EXPORT_FILE_CLEANUP_INTERVAL_HOURS = 6
DEFAULT_EXPORT_FILE_CLEANUP_BATCH_SIZE = 200
DEFAULT_BACKUP_CHECK_MAX_AGE_HOURS = 36
DEFAULT_BACKUP_PREFIX = "hotspot-db"


TERMINATE_CAUSE_ALIASES = {
    "user-request": "user_request",
    "user request": "user_request",
    "lost-service": "lost_service",
    "lost service": "lost_service",
    "lost-carrier": "lost_carrier",
    "lost carrier": "lost_carrier",
    "admin-reset": "admin_reset",
    "admin reset": "admin_reset",
    "session-timeout": "session_timeout",
    "session timeout": "session_timeout",
    "idle-timeout": "idle_timeout",
    "idle timeout": "idle_timeout",
    "nas-request": "nas_request",
    "nas request": "nas_request",
    "nas-reboot": "nas_reboot",
    "nas reboot": "nas_reboot",
    "legacy-cleanup": "cleanup_legacy",
    "retest-cleanup": "cleanup_retest",
    "cleanup": "cleanup",
    "duplicate-session": "duplicate_session",
    "duplicate session": "duplicate_session",
    "interim-timeout": "interim_timeout",
    "interim timeout": "interim_timeout",
    "stale-active-cleanup": "stale_active_cleanup",
    "stale active cleanup": "stale_active_cleanup",
    "stale-session-cleanup": "stale_session_cleanup",
    "stale session cleanup": "stale_session_cleanup",
    "duplicate-active-cleanup": "duplicate_active_cleanup",
    "duplicate active cleanup": "duplicate_active_cleanup",
    "manual-room-test-cleanup": "manual_room_test_cleanup",
    "manual room test cleanup": "manual_room_test_cleanup",
    "stale_session_cleanup": "stale_session_cleanup",
    "stale_active_cleanup": "stale_active_cleanup",
    "duplicate_active_cleanup": "duplicate_active_cleanup",
    "manual_room_test_cleanup": "manual_room_test_cleanup",
}


def normalize_terminate_cause(value: str | None) -> str | None:
    if value is None:
        return None

    raw = str(value).strip()
    if not raw:
        return None

    key = raw.lower().replace("_", "-")
    return TERMINATE_CAUSE_ALIASES.get(key, "unknown")


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
    logger.info(
        "AUDIT event_type=%s phone=%s mac=%s ip=%s nas_id=%s hotel=%s ssid=%s vlan_id=%s details=%s",
        event_type,
        phone or "-",
        mac or "-",
        ip or "-",
        nas_id or "-",
        hotel or "-",
        ssid or "-",
        vlan_id or "-",
        details or "-",
    )

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

    if evt.acct_status_type == "Interim-Update":
        if evt.acct_session_id:
            conn.execute("""
                UPDATE guest_sessions
                SET last_seen_at = ?,
                    ip = COALESCE(?, ip),
                    nas_id = COALESCE(?, nas_id),
                    acct_session_time = COALESCE(?, acct_session_time)
                WHERE acct_session_id = ?
                  AND status = 'active'
                  AND ended_at IS NULL
                  AND (
                        last_seen_at IS NULL
                     OR datetime(last_seen_at) <= datetime(?, '-120 seconds')
                  )
            """, (
                event_time,
                evt.ip,
                evt.nas_id,
                evt.session_time,
                evt.acct_session_id,
                event_time,
            ))

        conn.commit()
        conn.close()
        return


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
            terminate_cause_raw,
            session_time,
            event_time,
            raw_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        normalize_terminate_cause(evt.terminate_cause),
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
                  AND phone = ?
                  AND mac = ?
                ORDER BY id DESC
                LIMIT 1
            """, (phone, mac)).fetchone()

        if not row and mac:
            row = conn.execute("""
                SELECT id
                FROM guest_sessions
                WHERE status = 'active'
                  AND ended_at IS NULL
                  AND mac = ?
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
                    ended_at = NULL,
                    terminate_cause = NULL,
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


    elif evt.acct_status_type == "Stop":
        updated = 0
        if evt.acct_session_id:
            cur = conn.execute("""
                UPDATE guest_sessions
                SET ended_at = ?,
                    last_seen_at = ?,
                    status = 'closed',
                    terminate_cause = ?,
                    terminate_cause_raw = ?,
                    acct_session_time = COALESCE(?, acct_session_time)
                WHERE acct_session_id = ? AND status = 'active'
            """, (
                event_time,
                event_time,
                normalize_terminate_cause(evt.terminate_cause),
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
                    terminate_cause_raw = ?,
                    acct_session_time = COALESCE(?, acct_session_time)
                WHERE mac = ? AND status = 'active'
            """, (
                event_time,
                event_time,
                normalize_terminate_cause(evt.terminate_cause),
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
            result = run_cleanup()

            if any(result.values()):
                logger.info("cleanup result: %s", result)           

        except Exception:
            logger.exception("cleanup worker iteration failed")
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


def _int_setting(key: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        value = int(get_setting(key, default))
    except Exception:
        value = default

    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)

    return value


def get_retention_config() -> dict:
    return {
        "enabled": str(get_setting("retention.enabled", "1")) == "1",
        "days": _int_setting(
            "retention.days",
            DEFAULT_RETENTION_DAYS,
            minimum=MIN_RETENTION_DAYS,
            maximum=3650,
        ),
        "batch_size": _int_setting(
            "retention.batch_size",
            DEFAULT_RETENTION_BATCH_SIZE,
            minimum=100,
            maximum=50000,
        ),
        "interval_hours": _int_setting(
            "retention.interval_hours",
            DEFAULT_RETENTION_INTERVAL_HOURS,
            minimum=1,
            maximum=168,
        ),
        "last_run": str(get_setting("retention.last_run", "") or ""),
    }


def get_export_file_cleanup_config() -> dict:
    return {
        "enabled": str(get_setting("export.cleanup_enabled", "1")) == "1",
        "days": _int_setting(
            "export.files_retention_days",
            DEFAULT_EXPORT_FILE_RETENTION_DAYS,
            minimum=1,
            maximum=30,
        ),
        "batch_size": _int_setting(
            "export.cleanup_batch_size",
            DEFAULT_EXPORT_FILE_CLEANUP_BATCH_SIZE,
            minimum=10,
            maximum=5000,
        ),
        "interval_hours": _int_setting(
            "export.cleanup_interval_hours",
            DEFAULT_EXPORT_FILE_CLEANUP_INTERVAL_HOURS,
            minimum=1,
            maximum=168,
        ),
        "last_run": str(get_setting("export.cleanup_last_run", "") or ""),
    }


def _resolve_app_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return BASE_DIR / path


def get_backup_check_config() -> dict:
    return {
        "enabled": str(get_setting("backup.check_enabled", "1")) == "1",
        "dest_dir": str(get_setting("backup.dest_dir", str(BASE_DIR / "backups" / "db")) or ""),
        "prefix": str(get_setting("backup.prefix", DEFAULT_BACKUP_PREFIX) or DEFAULT_BACKUP_PREFIX),
        "max_age_hours": _int_setting(
            "backup.max_age_hours",
            DEFAULT_BACKUP_CHECK_MAX_AGE_HOURS,
            minimum=1,
            maximum=168,
        ),
    }


def _human_bytes(value: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"


def _find_latest_backup_db(dest_dir: Path, prefix: str) -> tuple[Path | None, Path | None]:
    if not dest_dir.exists() or not dest_dir.is_dir():
        return None, None

    backup_dirs = sorted(
        (
            path
            for path in dest_dir.iterdir()
            if path.is_dir() and path.name.startswith(f"{prefix}-")
        ),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    if not backup_dirs:
        return None, None

    db_name = Path(DB_PATH).name
    for backup_dir in backup_dirs:
        expected = backup_dir / db_name
        if expected.is_file():
            return backup_dir, expected

        db_candidates = sorted(
            path
            for path in backup_dir.iterdir()
            if path.is_file()
            and path.suffix.lower() in (".db", ".sqlite", ".sqlite3")
            and not path.name.endswith(("-wal", "-shm"))
        )
        if db_candidates:
            return backup_dir, db_candidates[0]

    return backup_dirs[0], None


def _sqlite_integrity_status(path: Path) -> dict:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            rows = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
        finally:
            conn.close()
    except Exception as exc:
        return {"ok": False, "text": f"integrity error: {exc}"}

    if rows == ["ok"]:
        return {"ok": True, "text": "integrity ok"}

    return {"ok": False, "text": "; ".join(rows[:3]) or "integrity failed"}


def get_backup_status(now_dt: datetime | None = None) -> dict:
    config = get_backup_check_config()
    if not config["enabled"]:
        return {
            "status": "warn",
            "details": "backup check disabled",
            "enabled": False,
        }

    dest_dir = _resolve_app_path(config["dest_dir"])
    current = now_dt or datetime.now(timezone.utc)
    backup_dir, backup_db = _find_latest_backup_db(dest_dir, config["prefix"])

    if backup_db is None:
        return {
            "status": "warn",
            "details": f"no backup database found in {dest_dir}",
            "enabled": True,
        }

    try:
        stat = backup_db.stat()
    except OSError as exc:
        return {
            "status": "bad",
            "details": f"backup database is not readable: {exc}",
            "enabled": True,
            "backup_db": str(backup_db),
        }

    backup_dt = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
    age_hours = max(0.0, (current - backup_dt).total_seconds() / 3600)
    integrity = _sqlite_integrity_status(backup_db)
    fresh = age_hours <= int(config["max_age_hours"])

    status = "ok"
    problems = []
    if not integrity["ok"]:
        status = "bad"
        problems.append(integrity["text"])
    if not fresh:
        status = "warn" if status == "ok" else status
        problems.append(f"age {age_hours:.1f}h > {int(config['max_age_hours'])}h")

    details_parts = [
        f"last {backup_dt.isoformat()}",
        f"age {age_hours:.1f}h",
        _human_bytes(stat.st_size),
        integrity["text"],
    ]
    if problems:
        details_parts.append("attention: " + ", ".join(problems))

    return {
        "status": status,
        "details": ", ".join(details_parts),
        "enabled": True,
        "dest_dir": str(dest_dir),
        "backup_dir": str(backup_dir or ""),
        "backup_db": str(backup_db),
        "backup_at": backup_dt.isoformat(),
        "age_hours": age_hours,
        "size_bytes": stat.st_size,
        "integrity_ok": integrity["ok"],
        "integrity": integrity["text"],
    }


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(str(value))
    except Exception:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed


def _retention_due(last_run: str, interval_hours: int) -> bool:
    parsed = _parse_dt(last_run)
    if parsed is None:
        return True

    return now() - parsed >= timedelta(hours=interval_hours)


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _delete_old_batch(conn, table: str, timestamp_expr: str, cutoff_iso: str, batch_size: int, extra_where: str = "") -> int:
    if not _table_exists(conn, table):
        return 0

    where_parts = []
    if extra_where:
        where_parts.append(f"({extra_where})")
    where_parts.append(f"{timestamp_expr} < ?")
    where_clause = " AND ".join(where_parts)

    cur = conn.execute(f"""
        DELETE FROM {table}
        WHERE id IN (
            SELECT id
            FROM {table}
            WHERE {where_clause}
            ORDER BY id
            LIMIT ?
        )
    """, (cutoff_iso, batch_size))

    return max(cur.rowcount or 0, 0)


def run_retention_cleanup(force: bool = False) -> dict:
    config = get_retention_config()
    if not config["enabled"] and not force:
        return {"ran": False, "deleted": 0}

    if not force and not _retention_due(config["last_run"], config["interval_hours"]):
        return {"ran": False, "deleted": 0}

    cutoff_iso = (now() - timedelta(days=config["days"])).isoformat()
    batch_size = config["batch_size"]
    stats = {
        "pending_auth": 0,
        "call_events": 0,
        "audit_log": 0,
        "radius_accounting": 0,
        "guest_sessions": 0,
        "room_auth": 0,
    }

    conn = db()
    try:
        stats["pending_auth"] = _delete_old_batch(
            conn,
            "pending_auth",
            "COALESCE(created_at, expires_at)",
            cutoff_iso,
            batch_size,
        )
        stats["call_events"] = _delete_old_batch(
            conn,
            "call_events",
            "created_at",
            cutoff_iso,
            batch_size,
        )
        stats["audit_log"] = _delete_old_batch(
            conn,
            "audit_log",
            "event_time",
            cutoff_iso,
            batch_size,
        )
        stats["radius_accounting"] = _delete_old_batch(
            conn,
            "radius_accounting",
            "event_time",
            cutoff_iso,
            batch_size,
        )
        stats["guest_sessions"] = _delete_old_batch(
            conn,
            "guest_sessions",
            "COALESCE(ended_at, last_seen_at, started_at)",
            cutoff_iso,
            batch_size,
            "status != 'active'",
        )
        stats["room_auth"] = _delete_old_batch(
            conn,
            "room_auth",
            "COALESCE(created_at, expires_at)",
            cutoff_iso,
            batch_size,
        )
        conn.commit()
    finally:
        conn.close()

    set_setting("retention.last_run", now_iso())

    total_deleted = sum(stats.values())
    return {
        "ran": True,
        "deleted": total_deleted,
        "cutoff": cutoff_iso,
        "tables": stats,
    }


def run_export_file_cleanup(force: bool = False) -> dict:
    config = get_export_file_cleanup_config()
    if not config["enabled"] and not force:
        return {"ran": False, "expired_jobs": 0, "files_deleted": 0}

    if not force and not _retention_due(config["last_run"], config["interval_hours"]):
        return {"ran": False, "expired_jobs": 0, "files_deleted": 0}

    stats = cleanup_export_job_files(
        BASE_DIR / "export_jobs",
        retention_days=config["days"],
        batch_size=config["batch_size"],
    )
    set_setting("export.cleanup_last_run", now_iso())

    return {
        "ran": True,
        "cutoff_days": config["days"],
        **stats,
    }


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
        SELECT id, started_at, last_seen_at
        FROM guest_sessions
        WHERE status = 'active'
          AND ended_at IS NULL
    """).fetchall()

    to_close = []

    for row in rows:
        raw_ts = row["last_seen_at"] or row["started_at"]

        try:
            last_seen = datetime.fromisoformat(raw_ts)
        except Exception:
            continue

        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)

        if current - last_seen > timedelta(hours=6):
            ended_at = last_seen.isoformat()
            to_close.append((ended_at, row["id"]))

    if to_close:
        conn.executemany("""
            UPDATE guest_sessions
            SET ended_at = ?,
                status = 'closed',
                terminate_cause = 'stale_session_cleanup'
            WHERE id = ?
        """, to_close)
        conn.commit()
        closed = len(to_close)

    conn.close()
    return closed


def run_cleanup():
    pending_stats = cleanup_db()
    stale_sessions_closed = cleanup_stale_sessions()
    retention_stats = run_retention_cleanup()
    export_cleanup_stats = run_export_file_cleanup()
    return {
        "pending_expired": pending_stats["expired"],
        "pending_deleted": pending_stats["deleted"],
        "stale_sessions_closed": stale_sessions_closed,
        "retention_checked": 1 if retention_stats.get("ran") else 0,
        "retention_deleted": int(retention_stats.get("deleted") or 0),
        "export_cleanup_checked": 1 if export_cleanup_stats.get("ran") else 0,
        "export_files_deleted": int(export_cleanup_stats.get("files_deleted") or 0),
        "export_jobs_expired": int(export_cleanup_stats.get("expired_jobs") or 0),
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
