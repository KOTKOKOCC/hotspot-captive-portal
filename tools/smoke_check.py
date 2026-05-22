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

    if args.base_url:
        check_http(args.base_url)


if __name__ == "__main__":
    main()
