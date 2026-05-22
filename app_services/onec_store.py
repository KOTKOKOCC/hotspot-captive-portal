import sqlite3
from datetime import datetime
from typing import Any

from config import DB_PATH
from app_services.settings_store import encrypt_secret, decrypt_secret


def _row_to_site(row):
    if not row:
        return None

    token = row["token"]

    return {
        "id": row["id"],
        "name": row["name"],
        "code": row["code"],
        "base_url": row["base_url"],
        "token": decrypt_secret(token) if token else "",
        "token_masked": "********" if token else "",
        "timeout": row["timeout"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }

def get_onec_site_by_name_or_code(value: str):
    value = (value or "").strip()
    if not value:
        return None

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    row = conn.execute("""
        SELECT *
        FROM onec_sites
        WHERE lower(code) = lower(?)
           OR lower(name) = lower(?)
        LIMIT 1
    """, (value, value)).fetchone()

    conn.close()

    if not row:
        return None

    return _row_to_site(row)


def init_onec_sites_table() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS onec_sites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                code TEXT NOT NULL UNIQUE,
                base_url TEXT NOT NULL DEFAULT '',
                token TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                timeout INTEGER NOT NULL DEFAULT 5,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()


def list_onec_sites() -> list[dict[str, Any]]:
    init_onec_sites_table()

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT id, name, code, base_url, token, enabled, timeout, created_at, updated_at
            FROM onec_sites
            ORDER BY name
        """).fetchall()

    result = []

    for row in rows:
        site_id, name, code, base_url, token, enabled, timeout, created_at, updated_at = row

        result.append({
            "id": site_id,
            "name": name,
            "code": code,
            "base_url": base_url,
            "token_masked": "********" if token else "",
            "enabled": bool(enabled),
            "timeout": timeout,
            "created_at": created_at,
            "updated_at": updated_at,
        })

    return result


def get_onec_site_by_code(code: str) -> dict[str, Any] | None:
    init_onec_sites_table()

    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("""
            SELECT id, name, code, base_url, token, enabled, timeout, created_at, updated_at
            FROM onec_sites
            WHERE code = ?
        """, (code,)).fetchone()

    if not row:
        return None

    site_id, name, code, base_url, token, enabled, timeout, created_at, updated_at = row

    return {
        "id": site_id,
        "name": name,
        "code": code,
        "base_url": base_url,
        "token": decrypt_secret(token) if token else "",
        "enabled": bool(enabled),
        "timeout": timeout,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def upsert_onec_site(
    name: str,
    code: str,
    base_url: str,
    token: str = "",
    enabled: bool = True,
    timeout: int = 5,
) -> None:
    init_onec_sites_table()

    now = datetime.utcnow().isoformat(timespec="seconds")
    code = code.strip()
    name = name.strip()
    base_url = base_url.strip()

    encrypted_token = encrypt_secret(token.strip()) if token.strip() else None

    with sqlite3.connect(DB_PATH) as conn:
        existing = conn.execute(
            "SELECT id, token FROM onec_sites WHERE code = ?",
            (code,),
        ).fetchone()

        if existing:
            site_id, old_token = existing
            final_token = encrypted_token if encrypted_token is not None else old_token

            conn.execute("""
                UPDATE onec_sites
                SET name = ?,
                    base_url = ?,
                    token = ?,
                    enabled = ?,
                    timeout = ?,
                    updated_at = ?
                WHERE id = ?
            """, (
                name,
                base_url,
                final_token,
                1 if enabled else 0,
                int(timeout),
                now,
                site_id,
            ))
        else:
            conn.execute("""
                INSERT INTO onec_sites (
                    name, code, base_url, token, enabled, timeout, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                name,
                code,
                base_url,
                encrypted_token or "",
                1 if enabled else 0,
                int(timeout),
                now,
                now,
            ))

        conn.commit()


def delete_onec_site(site_id: int) -> None:
    init_onec_sites_table()

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM onec_sites WHERE id = ?", (site_id,))
        conn.commit()
