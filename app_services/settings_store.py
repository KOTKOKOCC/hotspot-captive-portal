import json
import sqlite3
from datetime import datetime
from typing import Any

import base64
import hashlib
from cryptography.fernet import Fernet, InvalidToken

from config import DB_PATH, APP_SECRET


def init_settings_table() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                is_secret INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()


def get_setting(key: str, default: Any = None) -> Any:
    init_settings_table()

    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (key,),
        ).fetchone()

    if not row:
        return default

    raw = row[0]

    if raw.startswith(SECRET_PREFIX):
        value = decrypt_secret(raw)
        return default if value is None else value

    try:
        return json.loads(raw)
    except Exception:
        return raw


def set_setting(key: str, value: Any, is_secret: bool = False) -> None:
    init_settings_table()

    if is_secret:
        raw = encrypt_secret(value)
    else:
        raw = json.dumps(value, ensure_ascii=False)

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO app_settings (key, value, is_secret, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                is_secret = excluded.is_secret,
                updated_at = excluded.updated_at
        """, (
            key,
            raw,
            1 if is_secret else 0,
            datetime.utcnow().isoformat(timespec="seconds"),
        ))
        conn.commit()


def get_all_settings() -> dict[str, dict[str, Any]]:
    init_settings_table()

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT key, value, is_secret, updated_at
            FROM app_settings
            ORDER BY key
        """).fetchall()

    result = {}

    for key, raw, is_secret, updated_at in rows:
        if bool(is_secret):
            value = "********" if raw else ""
        else:
            try:
                value = json.loads(raw)
            except Exception:
                value = raw

        result[key] = {
            "value": value,
            "is_secret": bool(is_secret),
            "updated_at": updated_at,
        }

    return result


SECRET_PREFIX = "enc:"


def _get_fernet() -> Fernet:
    raw_key = hashlib.sha256(APP_SECRET.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(raw_key)
    return Fernet(key)


def encrypt_secret(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False)
    token = _get_fernet().encrypt(raw.encode("utf-8")).decode("utf-8")
    return SECRET_PREFIX + token


def decrypt_secret(raw: str) -> Any:
    if not raw.startswith(SECRET_PREFIX):
        try:
            return json.loads(raw)
        except Exception:
            return raw

    token = raw[len(SECRET_PREFIX):]

    try:
        decrypted = _get_fernet().decrypt(token.encode("utf-8")).decode("utf-8")
        return json.loads(decrypted)
    except InvalidToken:
        return None
