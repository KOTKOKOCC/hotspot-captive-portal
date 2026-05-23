#!/usr/bin/env python3
import argparse
import sys
import urllib.error
import urllib.request
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


CRITICAL_ROUTES = {
    ("POST", "/radius-check"),
    ("POST", "/radius-accounting"),
    ("POST", "/pbx-call"),
    ("GET", "/admin/login"),
    ("GET", "/admin/settings"),
    ("GET", "/admin/system"),
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fail(message: str) -> None:
    print(f"FAIL: {message}")
    raise SystemExit(1)


def ok(message: str) -> None:
    print(f"OK: {message}")


def warn(message: str) -> None:
    print(f"WARN: {message}")


def check_routes() -> None:
    from app import app

    route_map = []
    settings_routes = []

    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        endpoint_name = getattr(getattr(route, "endpoint", None), "__name__", "")

        for method in methods:
            route_map.append((method, path, endpoint_name))

        if path == "/admin/settings" and "GET" in methods:
            settings_routes.append(endpoint_name)

    missing = sorted(
        f"{method} {path}"
        for method, path in CRITICAL_ROUTES
        if (method, path) not in {(m, p) for m, p, _ in route_map}
    )
    if missing:
        fail("missing critical routes: " + ", ".join(missing))

    if settings_routes != ["admin_settings_page"]:
        fail(
            "/admin/settings must have one guarded GET endpoint, got: "
            + repr(settings_routes)
        )

    ok("critical routes are registered")
    ok("/admin/settings is guarded by admin_settings_page")


def check_secrets(strict: bool) -> None:
    import config
    import os
    from cryptography.fernet import Fernet

    defaults = []
    if config.APP_SECRET == "change_me":
        defaults.append("APP_SECRET")
    if config.ADMIN_PASSWORD == "change_me":
        defaults.append("ADMIN_PASSWORD")
    voucher_key = os.getenv("VOUCHER_SECRET_KEY", "").strip()
    if not voucher_key or voucher_key in ("change_me", "generated_by_setup"):
        defaults.append("VOUCHER_SECRET_KEY")

    invalid = []
    if voucher_key and voucher_key not in ("change_me", "generated_by_setup"):
        try:
            Fernet(voucher_key.encode("utf-8"))
        except Exception:
            invalid.append("VOUCHER_SECRET_KEY")

    if defaults and strict:
        fail("default secrets are not allowed in strict mode: " + ", ".join(defaults))
    if invalid:
        fail("invalid secret format: " + ", ".join(invalid))
    if defaults:
        warn("default local secrets detected: " + ", ".join(defaults))
    else:
        ok("application secrets are not default placeholders")


def check_admin_tokens() -> None:
    from admin_auth import (
        bootstrap_admin_users,
        ensure_admin_users_table,
        make_admin_token,
        parse_admin_token,
    )
    from config import ADMIN_USERNAME

    ensure_admin_users_table()
    bootstrap_admin_users()

    token = make_admin_token(ADMIN_USERNAME, "superadmin")
    username, role = parse_admin_token(token)
    if username != ADMIN_USERNAME or role != "superadmin":
        fail("fresh admin token was not accepted")

    if parse_admin_token(token + "x") != (None, None):
        fail("tampered admin token was accepted")

    expired = make_admin_token(ADMIN_USERNAME, "superadmin", ttl_seconds=-1)
    if parse_admin_token(expired) != (None, None):
        fail("expired admin token was accepted")

    if parse_admin_token(f"{'0' * 64}:{ADMIN_USERNAME}:superadmin") != (None, None):
        fail("legacy admin token was accepted")

    ok("admin session tokens are signed, expiring, and database-backed")


def check_opera_lookup_fallback() -> None:
    import sqlite3
    import tempfile
    from datetime import date, timedelta

    from integrations.opera import lookup

    original_db_path = lookup.DB_PATH
    lookup.DB_PATH = str(PROJECT_ROOT / ".missing" / "opera_stays.db")
    try:
        if lookup.room_auth_allowed("101", "Smith") is not False:
            fail("Opera lookup should fail closed when cache DB is unavailable")
    finally:
        lookup.DB_PATH = original_db_path

    ok("Opera room lookup fails closed when cache DB is unavailable")

    departure = (date.today() + timedelta(days=1)).strftime("%y%m%d")
    with tempfile.TemporaryDirectory() as tmp:
        test_db = Path(tmp) / "opera_stays.db"
        with sqlite3.connect(test_db) as conn:
            conn.execute("""
                CREATE TABLE opera_stays (
                    id INTEGER PRIMARY KEY,
                    room_num TEXT,
                    guest_surname_norm TEXT,
                    departure_date TEXT,
                    property_code TEXT,
                    status TEXT
                )
            """)
            conn.execute("""
                INSERT INTO opera_stays (
                    room_num, guest_surname_norm, departure_date, property_code, status
                )
                VALUES (?, ?, ?, ?, ?)
            """, ("101", "smith", departure, "DUSIT", "active"))
            conn.commit()

        lookup.DB_PATH = str(test_db)
        try:
            if not lookup.room_auth_allowed("101", "Smith"):
                fail("Opera lookup should match an active stay without property filter")
            if not lookup.room_auth_allowed("101", "Smith", property_code="DUSIT"):
                fail("Opera lookup should match an active stay with matching property")
            if lookup.room_auth_allowed("101", "Smith", property_code="OTHER"):
                fail("Opera lookup should reject an active stay from a different property")
        finally:
            lookup.DB_PATH = original_db_path

    ok("Opera room lookup respects property_code when cache DB supports it")


def check_optional_api_guard() -> None:
    from fastapi import HTTPException

    from api_security import ip_allowed, optional_api_guard

    class Client:
        def __init__(self, host: str):
            self.host = host

    class FakeRequest:
        def __init__(self, host: str, headers: dict[str, str] | None = None):
            self.client = Client(host)
            self.headers = headers or {}

    def assert_forbidden(fn, message: str) -> None:
        try:
            fn()
        except HTTPException as exc:
            if exc.status_code == 403:
                return
            fail(message + f": got status {exc.status_code}")
        fail(message)

    optional_api_guard(FakeRequest("10.0.0.5"), token="", allowed_ips=())

    optional_api_guard(
        FakeRequest("10.0.0.5", {"X-Internal-Token": "secret"}),
        token="secret",
        allowed_ips=(),
    )
    optional_api_guard(
        FakeRequest("10.0.0.5", {"Authorization": "Bearer secret"}),
        token="secret",
        allowed_ips=(),
    )

    assert_forbidden(
        lambda: optional_api_guard(FakeRequest("10.0.0.5"), token="secret", allowed_ips=()),
        "optional API guard accepted a missing token",
    )

    if not ip_allowed("10.0.0.5", ["10.0.0.0/24"]):
        fail("optional API guard CIDR allowlist did not match")
    if ip_allowed("10.0.1.5", ["10.0.0.0/24"]):
        fail("optional API guard CIDR allowlist matched wrong network")

    assert_forbidden(
        lambda: optional_api_guard(FakeRequest("10.0.1.5"), token="", allowed_ips=("10.0.0.0/24",)),
        "optional API guard accepted a forbidden IP",
    )

    ok("optional API guard supports token and IP allowlists")


def fetch_no_redirect(url: str):
    opener = urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(url, method="GET")
    try:
        response = opener.open(request, timeout=5)
        return response.status, response.headers.get("Location", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Location", "")


def check_http(base_url: str) -> None:
    base_url = base_url.rstrip("/")

    status, location = fetch_no_redirect(base_url + "/admin/login")
    if status != 200:
        fail(f"/admin/login expected 200, got {status}")
    ok("/admin/login returns 200")

    for path in ("/admin/settings", "/admin/system?section=settings"):
        status, location = fetch_no_redirect(base_url + path)
        if status != 303 or location != "/admin/login":
            fail(f"{path} expected 303 -> /admin/login, got {status} -> {location}")
        ok(f"{path} redirects unauthenticated users to /admin/login")


def main() -> None:
    parser = argparse.ArgumentParser(description="Hotspot portal smoke checks")
    parser.add_argument("--base-url", help="Optional running app URL, e.g. http://127.0.0.1:8080")
    parser.add_argument("--strict-secrets", action="store_true")
    args = parser.parse_args()

    check_routes()
    check_secrets(strict=args.strict_secrets)
    check_admin_tokens()
    check_opera_lookup_fallback()
    check_optional_api_guard()

    if args.base_url:
        check_http(args.base_url)


if __name__ == "__main__":
    main()
