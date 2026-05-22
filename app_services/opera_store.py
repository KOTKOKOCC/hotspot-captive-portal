import sqlite3
from datetime import datetime
from typing import Any

from config import DB_PATH
from app_services.settings_store import encrypt_secret, decrypt_secret


def init_opera_sites_table() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS opera_sites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                code TEXT NOT NULL UNIQUE,
                host TEXT NOT NULL DEFAULT '',
                port INTEGER NOT NULL DEFAULT 5057,
                use_ssl INTEGER NOT NULL DEFAULT 0,
                auth_key TEXT NOT NULL DEFAULT '',
                property_code TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                connect_timeout INTEGER NOT NULL DEFAULT 10,
                reconnect_seconds INTEGER NOT NULL DEFAULT 30,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()


def list_opera_sites() -> list[dict[str, Any]]:
    init_opera_sites_table()

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT id, name, code, host, port, use_ssl, auth_key, property_code,
                   enabled, connect_timeout, reconnect_seconds, created_at, updated_at
            FROM opera_sites
            ORDER BY name
        """).fetchall()

    result = []

    for row in rows:
        (
            site_id,
            name,
            code,
            host,
            port,
            use_ssl,
            auth_key,
            property_code,
            enabled,
            connect_timeout,
            reconnect_seconds,
            created_at,
            updated_at,
        ) = row

        result.append({
            "id": site_id,
            "name": name,
            "code": code,
            "host": host,
            "port": port,
            "use_ssl": bool(use_ssl),
            "auth_key_masked": "********" if auth_key else "",
            "property_code": property_code,
            "enabled": bool(enabled),
            "connect_timeout": connect_timeout,
            "reconnect_seconds": reconnect_seconds,
            "created_at": created_at,
            "updated_at": updated_at,
        })

    return result


def _opera_row_to_site(row):
    if not row:
        return None

    (
        site_id,
        name,
        code,
        host,
        port,
        use_ssl,
        auth_key,
        property_code,
        enabled,
        connect_timeout,
        reconnect_seconds,
        created_at,
        updated_at,
    ) = row

    return {
        "id": site_id,
        "name": name,
        "code": code,
        "host": host,
        "port": port,
        "use_ssl": bool(use_ssl),
        "auth_key": decrypt_secret(auth_key) if auth_key else "",
        "auth_key_masked": "********" if auth_key else "",
        "property_code": property_code,
        "enabled": bool(enabled),
        "connect_timeout": connect_timeout,
        "reconnect_seconds": reconnect_seconds,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def get_opera_site_by_name_or_code(value: str) -> dict[str, Any] | None:
    init_opera_sites_table()

    value = (value or "").strip()
    if not value:
        return None

    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("""
            SELECT id, name, code, host, port, use_ssl, auth_key, property_code,
                   enabled, connect_timeout, reconnect_seconds, created_at, updated_at
            FROM opera_sites
            WHERE lower(code) = lower(?)
               OR lower(name) = lower(?)
            LIMIT 1
        """, (value, value)).fetchone()

    return _opera_row_to_site(row)


def get_opera_site_by_code(code: str) -> dict[str, Any] | None:
    init_opera_sites_table()

    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("""
            SELECT id, name, code, host, port, use_ssl, auth_key, property_code,
                   enabled, connect_timeout, reconnect_seconds, created_at, updated_at
            FROM opera_sites
            WHERE code = ?
        """, (code,)).fetchone()

    if not row:
        return None

    (
        site_id,
        name,
        code,
        host,
        port,
        use_ssl,
        auth_key,
        property_code,
        enabled,
        connect_timeout,
        reconnect_seconds,
        created_at,
        updated_at,
    ) = row

    return {
        "id": site_id,
        "name": name,
        "code": code,
        "host": host,
        "port": port,
        "use_ssl": bool(use_ssl),
        "auth_key": decrypt_secret(auth_key) if auth_key else "",
        "property_code": property_code,
        "enabled": bool(enabled),
        "connect_timeout": connect_timeout,
        "reconnect_seconds": reconnect_seconds,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def upsert_opera_site(
    name: str,
    code: str,
    host: str,
    port: int = 5057,
    use_ssl: bool = False,
    auth_key: str = "",
    property_code: str = "",
    enabled: bool = True,
    connect_timeout: int = 10,
    reconnect_seconds: int = 30,
) -> None:
    init_opera_sites_table()

    now = datetime.utcnow().isoformat(timespec="seconds")

    name = name.strip()
    code = code.strip()
    host = host.strip()
    property_code = property_code.strip()

    encrypted_auth_key = encrypt_secret(auth_key.strip()) if auth_key.strip() else None

    with sqlite3.connect(DB_PATH) as conn:
        existing = conn.execute(
            "SELECT id, auth_key FROM opera_sites WHERE code = ?",
            (code,),
        ).fetchone()

        if existing:
            site_id, old_auth_key = existing
            final_auth_key = encrypted_auth_key if encrypted_auth_key is not None else old_auth_key

            conn.execute("""
                UPDATE opera_sites
                SET name = ?,
                    host = ?,
                    port = ?,
                    use_ssl = ?,
                    auth_key = ?,
                    property_code = ?,
                    enabled = ?,
                    connect_timeout = ?,
                    reconnect_seconds = ?,
                    updated_at = ?
                WHERE id = ?
            """, (
                name,
                host,
                int(port),
                1 if use_ssl else 0,
                final_auth_key,
                property_code,
                1 if enabled else 0,
                int(connect_timeout),
                int(reconnect_seconds),
                now,
                site_id,
            ))
        else:
            conn.execute("""
                INSERT INTO opera_sites (
                    name, code, host, port, use_ssl, auth_key, property_code,
                    enabled, connect_timeout, reconnect_seconds, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                name,
                code,
                host,
                int(port),
                1 if use_ssl else 0,
                encrypted_auth_key or "",
                property_code,
                1 if enabled else 0,
                int(connect_timeout),
                int(reconnect_seconds),
                now,
                now,
            ))

        conn.commit()


def delete_opera_site(site_id: int) -> None:
    init_opera_sites_table()

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM opera_sites WHERE id = ?", (site_id,))
        conn.commit()
