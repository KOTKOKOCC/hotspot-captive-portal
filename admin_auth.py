import hashlib
import hmac
import base64
import json
import os
import time
from datetime import datetime, timezone

from fastapi import Request
from fastapi.responses import RedirectResponse

from config import APP_SECRET, ADMIN_COOKIE


ADMIN_SESSION_TTL_SECONDS = int(os.getenv("ADMIN_SESSION_TTL_SECONDS", str(60 * 60 * 8)))


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + padding)


def _sign_payload(payload: str) -> str:
    digest = hmac.new(
        APP_SECRET.encode("utf-8"),
        payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return _b64encode(digest)


def make_admin_token(username: str, role: str, ttl_seconds: int | None = None) -> str:
    ttl = ADMIN_SESSION_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
    payload = {
        "username": username,
        "role": role,
        "expires_at": int(time.time()) + ttl,
    }
    payload_raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    payload_b64 = _b64encode(payload_raw)
    return f"v2.{payload_b64}.{_sign_payload(payload_b64)}"


def _active_admin_role(username: str) -> str | None:
    from db import fetch_one

    row = fetch_one(
        """
        SELECT role
        FROM admin_users
        WHERE username = ?
          AND is_active = 1
        """,
        (username,),
    )
    return row["role"] if row else None


def parse_admin_token(token: str):
    if not token or not token.startswith("v2."):
        return None, None

    try:
        _, payload_b64, signature = token.split(".", 2)
        expected = _sign_payload(payload_b64)

        if not hmac.compare_digest(signature, expected):
            return None, None

        payload = json.loads(_b64decode(payload_b64).decode("utf-8"))
        username = str(payload.get("username") or "")
        role = str(payload.get("role") or "")
        expires_at = int(payload.get("expires_at") or 0)

        if not username or not role or time.time() > expires_at:
            return None, None

        active_role = _active_admin_role(username)
        if active_role != role:
            return None, None

        return username, active_role
    except Exception:
        return None, None



def get_current_admin_user(request: Request):
    token = request.cookies.get(ADMIN_COOKIE)
    return parse_admin_token(token)


def admin_guard(request: Request):
    username, role = get_current_admin_user(request)

    if not username:
        return RedirectResponse(url="/admin/login", status_code=303)

    if role not in ("admin", "superadmin"):
        return RedirectResponse(url="/admin?denied=1", status_code=303)
        
    return None


def role_guard(request: Request, allowed_roles: tuple[str, ...]):
    username, role = get_current_admin_user(request)

    if not username:
        return RedirectResponse(url="/admin/login", status_code=303)

    if role not in allowed_roles:
        return RedirectResponse(url="/admin?denied=1", status_code=303)

    return None






def hash_admin_password(password: str) -> str:
    iterations = 200_000
    salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations
    ).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def verify_admin_password(password: str, stored_hash: str) -> bool:
    try:
        algo, iterations_raw, salt, digest = stored_hash.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False

        iterations = int(iterations_raw)
        check = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            iterations
        ).hex()

        return hmac.compare_digest(check, digest)
    except Exception:
        return False


def ensure_admin_users_table():
    from db import db

    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'it',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def bootstrap_admin_users():
    from db import db
    from config import ADMIN_USERNAME, ADMIN_PASSWORD


    now = datetime.now(timezone.utc).isoformat()

    conn = db()

    def ensure_user(username: str, password: str, role: str):
        if not username or not password:
            return

        exists = conn.execute(
            "SELECT id FROM admin_users WHERE username = ?",
            (username,)
        ).fetchone()

        if exists:
            return

        conn.execute("""
            INSERT INTO admin_users (
                username, password_hash, role, is_active, created_at, updated_at
            )
            VALUES (?, ?, ?, 1, ?, ?)
        """, (
            username,
            hash_admin_password(password),
            role,
            now,
            now
        ))

    ensure_user(ADMIN_USERNAME, ADMIN_PASSWORD, "superadmin")


    conn.commit()
    conn.close()
