from hashlib import sha256

from fastapi import Request
from fastapi.responses import RedirectResponse

from config import APP_SECRET, ADMIN_USERNAME, ADMIN_PASSWORD, ADMIN_COOKIE


def make_admin_token(username: str) -> str:
    raw = f"{username}:{APP_SECRET}"
    return sha256(raw.encode()).hexdigest()

def check_admin_token(token: str) -> bool:
    expected = make_admin_token(ADMIN_USERNAME)
    return token == expected

def admin_guard(request: Request):
    token = request.cookies.get(ADMIN_COOKIE)
    if not check_admin_token(token):
        return RedirectResponse(url="/admin/login", status_code=303)
    return None