import hashlib
import hmac
import os
from datetime import datetime, timezone

from hashlib import sha256

from fastapi import Request
from fastapi.responses import RedirectResponse, HTMLResponse

from config import APP_SECRET, ADMIN_USERNAME, ADMIN_PASSWORD, ADMIN_COOKIE


def make_admin_token(username: str, role: str) -> str:
    raw = f"{username}:{role}:{APP_SECRET}"
    sig = sha256(raw.encode()).hexdigest()
    return f"{sig}:{username}:{role}"


def parse_admin_token(token: str):
    if not token or ":" not in token:
        return None, None

    try:
        sig, username, role = token.split(":", 2)
        raw = f"{username}:{role}:{APP_SECRET}"
        expected = sha256(raw.encode()).hexdigest()

        if sig != expected:
            return None, None

        return username, role
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

