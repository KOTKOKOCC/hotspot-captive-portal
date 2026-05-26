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
    ("POST", "/admin/settings/pms-check"),
    ("POST", "/admin/export/jobs"),
    ("GET", "/admin/export/jobs/{job_id}/download"),
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


def check_admin_role_access_matrix() -> None:
    import tempfile
    from datetime import datetime, timezone

    import db as db_module
    from admin_auth import (
        ROLE_IT,
        ROLE_RECEPTION,
        ROLE_SUPERADMIN,
        ensure_admin_users_table,
        hash_admin_password,
        make_admin_token,
        normalize_admin_role,
        parse_admin_token,
        role_guard,
    )
    from config import ADMIN_COOKIE
    from ui import admin_page

    original_db_path = db_module.DB_PATH

    class FakeRequest:
        def __init__(self, token: str):
            self.cookies = {ADMIN_COOKIE: token}

    def assert_denied_location(response, expected_location: str, label: str) -> None:
        if response is None:
            fail(f"{label} should be denied")
        if response.status_code != 303 or response.headers.get("location") != expected_location:
            fail(
                f"{label} expected 303 -> {expected_location}, "
                f"got {response.status_code} -> {response.headers.get('location')}"
            )

    with tempfile.TemporaryDirectory() as tmp:
        test_db = Path(tmp) / "roles.db"
        db_module.DB_PATH = str(test_db)

        try:
            ensure_admin_users_table()
            now_dt = datetime.now(timezone.utc)
            now = now_dt.isoformat()
            conn = db_module.db()
            for username, role in (
                ("smoke_super", ROLE_SUPERADMIN),
                ("smoke_it", ROLE_IT),
                ("smoke_reception", ROLE_RECEPTION),
                ("smoke_reseption", "reseption"),
            ):
                conn.execute(
                    """
                    INSERT INTO admin_users (
                        username, password_hash, role, is_active, created_at, updated_at
                    )
                    VALUES (?, ?, ?, 1, ?, ?)
                    """,
                    (username, hash_admin_password("password"), role, now, now),
                )
            conn.commit()
            conn.close()

            super_req = FakeRequest(make_admin_token("smoke_super", ROLE_SUPERADMIN))
            it_req = FakeRequest(make_admin_token("smoke_it", ROLE_IT))
            reception_req = FakeRequest(make_admin_token("smoke_reception", ROLE_RECEPTION))
            reseption_req = FakeRequest(make_admin_token("smoke_reseption", "reseption"))

            if role_guard(super_req, (ROLE_SUPERADMIN,)) is not None:
                fail("superadmin should pass superadmin-only guard")
            assert_denied_location(
                role_guard(it_req, (ROLE_SUPERADMIN,)),
                "/admin?denied=1",
                "it on superadmin-only guard",
            )
            assert_denied_location(
                role_guard(reception_req, (ROLE_SUPERADMIN,)),
                "/admin/vouchers?denied=1",
                "reception on superadmin-only guard",
            )
            if role_guard(reseption_req, (ROLE_RECEPTION,)) is not None:
                fail("reseption alias should pass reception guard")

            from app import (
                admin_export_jobs_create,
                admin_system_logs_stream,
                admin_system_service_status_json,
                admin_voucher_remove_device,
                admin_vouchers_create,
            )

            assert_denied_location(
                admin_vouchers_create(
                    it_req,
                    full_name="Иванов Иван",
                    passport="1234567890",
                    birth_date="",
                    phone="",
                    site="",
                    room_num="",
                    max_devices=1,
                    valid_days=1,
                    document_type="rf_passport",
                ),
                "/admin?denied=1",
                "it on voucher creation",
            )
            assert_denied_location(
                admin_voucher_remove_device(reception_req, voucher_id=1, mac="AA:BB:CC:DD:EE:FF"),
                "/admin/vouchers?denied=1",
                "reception on voucher device removal",
            )
            assert_denied_location(
                admin_export_jobs_create(it_req),
                "/admin?denied=1",
                "it on export creation",
            )
            assert_denied_location(
                admin_system_logs_stream(it_req),
                "/admin?denied=1",
                "it on live logs stream",
            )
            status_json = admin_system_service_status_json(it_req)
            if status_json != {"ok": False, "error": "forbidden"}:
                fail("it should not access system service status JSON")

            alias_username, alias_role = parse_admin_token(make_admin_token("smoke_reseption", "reseption"))
            if alias_username != "smoke_reseption" or alias_role != ROLE_RECEPTION:
                fail("reseption alias was not normalized to reception")

            if normalize_admin_role("reseption") != ROLE_RECEPTION:
                fail("role normalizer did not accept reseption alias")

            reception_html = admin_page("Тест", "", role="reseption").body.decode("utf-8")
            if "/admin/vouchers" not in reception_html or "/admin/guests" in reception_html:
                fail("reception navigation should contain only vouchers and logout")

            it_html = admin_page("Тест", "", role=ROLE_IT).body.decode("utf-8")
            if "/admin/system" in it_html or "/admin/audit" in it_html:
                fail("it navigation should not expose system or audit")

            super_html = admin_page("Тест", "", role=ROLE_SUPERADMIN).body.decode("utf-8")
            if "/admin/system" not in super_html or "/admin/audit" not in super_html:
                fail("superadmin navigation should expose system and audit")
        finally:
            db_module.DB_PATH = original_db_path

    ok("admin role access matrix keeps reception, it, and superadmin separated")


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

    from api_security import ip_allowed, optional_api_guard, require_api_guard

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

    assert_forbidden(
        lambda: require_api_guard(FakeRequest("10.0.0.5"), token="", allowed_ips=()),
        "required API guard allowed traffic without token or IP allowlist",
    )

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


def check_internal_api_guards() -> None:
    from fastapi import HTTPException

    import app as app_module

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

    values = {}
    original_get_setting = app_module.get_setting
    original_audit = app_module.audit

    def fake_get_setting(key, default=None):
        return values.get(key, default)

    def fake_audit(*args, **kwargs):
        return None

    app_module.get_setting = fake_get_setting
    app_module.audit = fake_audit
    try:
        values.clear()
        app_module.radius_api_guard(FakeRequest("127.0.0.1"))
        assert_forbidden(
            lambda: app_module.radius_api_guard(FakeRequest("10.0.0.5")),
            "RADIUS guard accepted a non-local IP by default",
        )

        values.clear()
        values.update({"radius.allowed_ips": ""})
        assert_forbidden(
            lambda: app_module.radius_api_guard(FakeRequest("127.0.0.1")),
            "RADIUS guard accepted traffic with an empty allowlist",
        )

        values.clear()
        values.update({"radius.allowed_ips": "10.0.0.0/24"})
        app_module.radius_api_guard(FakeRequest("10.0.0.5"))
        assert_forbidden(
            lambda: app_module.radius_api_guard(FakeRequest("10.0.1.5")),
            "RADIUS guard accepted an IP outside the configured CIDR",
        )

        values.clear()
        values.update({"pbx.enabled": "1", "pbx.allowed_ips": ""})
        assert_forbidden(
            lambda: app_module.pbx_api_guard(FakeRequest("10.0.0.5")),
            "PBX guard accepted traffic with an empty allowlist",
        )

        values.clear()
        values.update({"pbx.enabled": "1", "pbx.allowed_ips": "10.0.0.0/24"})
        app_module.pbx_api_guard(FakeRequest("10.0.0.5"))
        assert_forbidden(
            lambda: app_module.pbx_api_guard(FakeRequest("10.0.1.5")),
            "PBX guard accepted an IP outside the configured CIDR",
        )

        values.clear()
        values.update({"pbx.enabled": "0", "pbx.allowed_ips": "10.0.0.0/24"})
        assert_forbidden(
            lambda: app_module.pbx_api_guard(FakeRequest("10.0.0.5")),
            "PBX guard accepted traffic while disabled",
        )
    finally:
        app_module.get_setting = original_get_setting
        app_module.audit = original_audit

    ok("internal RADIUS and PBX guards fail closed and support CIDR allowlists")


def check_pms_api_guard_settings() -> None:
    from fastapi import HTTPException

    import app as app_module

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

    values = {}
    original_get_setting = app_module.get_setting

    def fake_get_setting(key, default=None):
        return values.get(key, default)

    app_module.get_setting = fake_get_setting
    try:
        values.clear()
        values.update({"pms_api.enabled": "0"})
        app_module.pms_api_guard(FakeRequest("10.0.0.5"))

        values.clear()
        values.update({
            "pms_api.enabled": "1",
            "pms_api.token": "",
            "pms_api.allowed_ips": "",
        })
        assert_forbidden(
            lambda: app_module.pms_api_guard(FakeRequest("10.0.0.5")),
            "PMS API guard allowed traffic while enabled without token or IP allowlist",
        )

        values.clear()
        values.update({
            "pms_api.enabled": "1",
            "pms_api.token": "secret",
            "pms_api.allowed_ips": "",
        })
        app_module.pms_api_guard(
            FakeRequest("10.0.0.5", {"Authorization": "Bearer secret"})
        )
    finally:
        app_module.get_setting = original_get_setting

    ok("PMS API guard uses UI/database settings and fails closed")


def check_legacy_dusit_routes_use_pms_router() -> None:
    import app as app_module

    class Client:
        host = "127.0.0.1"

    class FakeRequest:
        client = Client()
        headers = {}

    original_get_setting = app_module.get_setting
    original_router = app_module.pms_room_auth_allowed
    original_save = app_module.save_verified_room_auth

    calls = []
    saved = []

    def fake_get_setting(key, default=None):
        if key == "pms_api.enabled":
            return "0"
        return default

    def fake_router(room_num, surname, hotel=None, vlan_id=None):
        calls.append({
            "room_num": room_num,
            "surname": surname,
            "hotel": hotel,
            "vlan_id": vlan_id,
        })
        return {"ok": True, "source": "opera", "hotel": hotel, "error": ""}

    def fake_save_verified_room_auth(**kwargs):
        saved.append(kwargs)

    app_module.get_setting = fake_get_setting
    app_module.pms_room_auth_allowed = fake_router
    app_module.save_verified_room_auth = fake_save_verified_room_auth
    try:
        response = app_module.auth_dusit_room(
            FakeRequest(),
            {"room_num": "101", "surname": "Smith"},
        )
        if response.get("ok") is not True or response.get("source") != "opera":
            fail("legacy Dusit room route did not accept PMS router result")

        response = app_module.auth_dusit_authorize(
            FakeRequest(),
            {
                "room_num": "101",
                "surname": "Smith",
                "mac": "AA:BB:CC:DD:EE:FF",
                "ip": "10.0.0.5",
                "nas_id": "nas-1",
            },
        )
        if response.get("ok") is not True or not saved:
            fail("legacy Dusit authorize route did not use PMS router result")

        if any(call.get("hotel") != "Dusit" for call in calls):
            fail("legacy Dusit routes did not restrict PMS lookup to configured Dusit site")
    finally:
        app_module.get_setting = original_get_setting
        app_module.pms_room_auth_allowed = original_router
        app_module.save_verified_room_auth = original_save

    ok("legacy Dusit routes use the configured PMS router")


def check_pms_check_result_renderer() -> None:
    from app import build_pms_check_result_html

    found_html = build_pms_check_result_html({
        "hotel": "FioLeto",
        "room_num": "101",
        "surname": "Smith",
        "result": {"ok": True, "source": "1c", "error": ""},
    })
    if "Гость найден" not in found_html or "Портал пустит гостя" not in found_html:
        fail("PMS check renderer did not render successful guest result")

    missing_html = build_pms_check_result_html({
        "hotel": "FioLeto",
        "room_num": "101",
        "surname": "Smith",
        "result": {"ok": False, "source": "1c", "error": "guest_not_found"},
    })
    if "Гость не найден" not in missing_html:
        fail("PMS check renderer did not render guest_not_found result")

    config_html = build_pms_check_result_html({
        "hotel": "Unknown",
        "room_num": "101",
        "surname": "Smith",
        "result": {"ok": False, "source": None, "error": "pms_not_configured_for_hotel"},
    })
    if "PMS не готов к проверке" not in config_html:
        fail("PMS check renderer did not render configuration failure")

    ok("PMS check renderer explains found, not found, and config states")


def check_reauth_window_setting() -> None:
    import tempfile
    from datetime import datetime, timedelta, timezone

    import auth as auth_module
    import db as db_module
    import app_services.settings_store as settings_store
    from app_services.settings_store import set_setting
    from db import init_db

    original_db_path = db_module.DB_PATH
    original_settings_db_path = settings_store.DB_PATH

    old_but_allowed = (datetime.now(timezone.utc) - timedelta(days=3, hours=12)).isoformat()

    with tempfile.TemporaryDirectory() as tmp:
        test_db = Path(tmp) / "reauth.db"
        db_module.DB_PATH = str(test_db)
        settings_store.DB_PATH = str(test_db)

        try:
            init_db()
            conn = db_module.db()
            conn.execute("""
                INSERT INTO guests (
                    phone, first_verified_at, first_hotel, auth_method,
                    status, created_at, updated_at, last_auth_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "79990000444",
                old_but_allowed,
                "FioLeto",
                "call",
                "active",
                old_but_allowed,
                old_but_allowed,
                old_but_allowed,
            ))
            guest_id = conn.execute("SELECT id FROM guests WHERE phone = ?", ("79990000444",)).fetchone()["id"]
            conn.execute("""
                INSERT INTO guest_sessions (
                    guest_id, phone, mac, ip, nas_id, hotel, ssid, vlan_id,
                    started_at, last_seen_at, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                guest_id,
                "79990000444",
                "AA:BB:CC:DD:EE:44",
                "10.32.0.44",
                "nas",
                "FioLeto",
                "MIRACLEON",
                "302",
                old_but_allowed,
                old_but_allowed,
                "closed",
            ))
            conn.commit()
            conn.close()

            set_setting("auth.reauth_days", 4)
            if auth_module.get_active_guest("79990000444") is None:
                fail("reauth window setting did not allow a 3.5-day phone identity")
            if auth_module.get_recent_authorized_session_by_mac("AA:BB:CC:DD:EE:44") is None:
                fail("reauth window setting did not allow a 3.5-day MAC identity")

            set_setting("auth.reauth_days", 3)
            if auth_module.get_active_guest("79990000444") is not None:
                fail("reauth window setting accepted a phone identity outside 3 days")
            if auth_module.get_recent_authorized_session_by_mac("AA:BB:CC:DD:EE:44") is not None:
                fail("reauth window setting accepted a MAC identity outside 3 days")

            set_setting("auth.reauth_days", 999)
            if auth_module.get_reauth_window_days() != auth_module.MAX_REAUTH_DAYS:
                fail("reauth window setting did not clamp high values")
        finally:
            db_module.DB_PATH = original_db_path
            settings_store.DB_PATH = original_settings_db_path

    ok("guest reauthorization window is configurable and affects phone and MAC auth")


def check_retention_cleanup() -> None:
    import tempfile
    from datetime import datetime, timedelta, timezone

    import db as db_module
    import app_services.settings_store as settings_store
    import services
    from app_services.settings_store import get_setting, set_setting
    from db import init_db
    from room_auth import ensure_room_auth_table

    original_db_path = db_module.DB_PATH
    original_settings_db_path = settings_store.DB_PATH

    old_ts = (datetime.now(timezone.utc) - timedelta(days=181)).isoformat()
    recent_ts = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()

    with tempfile.TemporaryDirectory() as tmp:
        test_db = Path(tmp) / "retention.db"
        db_module.DB_PATH = str(test_db)
        settings_store.DB_PATH = str(test_db)

        try:
            init_db()
            ensure_room_auth_table()
            set_setting("retention.enabled", "1")
            set_setting("retention.days", 30)
            set_setting("retention.batch_size", 1000)
            set_setting("retention.last_run", "")

            conn = db_module.db()
            conn.execute("""
                INSERT INTO pending_auth
                (phone, mac, ip, nas_id, hotel, ssid, vlan_id, created_at, expires_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, ("79990000181", "AA:BB:CC:DD:EE:01", "10.0.0.1", "nas", "hotel", "ssid", "1", old_ts, old_ts, "expired"))
            conn.execute("""
                INSERT INTO pending_auth
                (phone, mac, ip, nas_id, hotel, ssid, vlan_id, created_at, expires_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, ("79990000120", "AA:BB:CC:DD:EE:02", "10.0.0.2", "nas", "hotel", "ssid", "1", recent_ts, recent_ts, "expired"))
            conn.execute("""
                INSERT INTO call_events (phone, callerid_raw, source_ip, created_at, result)
                VALUES (?, ?, ?, ?, ?)
            """, ("79990000181", "79990000181", "10.0.0.1", old_ts, "smoke_old"))
            conn.execute("""
                INSERT INTO call_events (phone, callerid_raw, source_ip, created_at, result)
                VALUES (?, ?, ?, ?, ?)
            """, ("79990000120", "79990000120", "10.0.0.2", recent_ts, "smoke_recent"))
            conn.execute("""
                INSERT INTO audit_log (phone, mac, ip, nas_id, hotel, ssid, vlan_id, event_type, event_time, details)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, ("79990000181", "AA:BB:CC:DD:EE:01", "10.0.0.1", "nas", "hotel", "ssid", "1", "smoke_old", old_ts, "old"))
            conn.execute("""
                INSERT INTO audit_log (phone, mac, ip, nas_id, hotel, ssid, vlan_id, event_type, event_time, details)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, ("79990000120", "AA:BB:CC:DD:EE:02", "10.0.0.2", "nas", "hotel", "ssid", "1", "smoke_recent", recent_ts, "recent"))
            conn.execute("""
                INSERT INTO radius_accounting
                (acct_session_id, username, mac, ip, nas_ip, nas_id, nas_port_id, called_station_id, acct_status_type, event_time, raw_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, ("smoke-old", "79990000181", "AA:BB:CC:DD:EE:01", "10.0.0.1", "10.0.0.254", "nas", "port", "ssid", "Stop", old_ts, "{}", old_ts))
            conn.execute("""
                INSERT INTO radius_accounting
                (acct_session_id, username, mac, ip, nas_ip, nas_id, nas_port_id, called_station_id, acct_status_type, event_time, raw_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, ("smoke-recent", "79990000120", "AA:BB:CC:DD:EE:02", "10.0.0.2", "10.0.0.254", "nas", "port", "ssid", "Stop", recent_ts, "{}", recent_ts))
            conn.execute("""
                INSERT INTO guest_sessions
                (phone, mac, ip, nas_id, hotel, ssid, vlan_id, started_at, last_seen_at, ended_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, ("79990000181", "AA:BB:CC:DD:EE:01", "10.0.0.1", "nas", "hotel", "ssid", "1", old_ts, old_ts, old_ts, "closed"))
            conn.execute("""
                INSERT INTO guest_sessions
                (phone, mac, ip, nas_id, hotel, ssid, vlan_id, started_at, last_seen_at, ended_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, ("79990000120", "AA:BB:CC:DD:EE:02", "10.0.0.2", "nas", "hotel", "ssid", "1", recent_ts, recent_ts, recent_ts, "closed"))
            conn.execute("""
                INSERT INTO room_auth (room_num, surname, surname_norm, mac, ip, nas_id, hotel, status, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, ("101", "Old", "old", "AA:BB:CC:DD:EE:01", "10.0.0.1", "nas", "hotel", "verified", old_ts, old_ts))
            conn.execute("""
                INSERT INTO room_auth (room_num, surname, surname_norm, mac, ip, nas_id, hotel, status, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, ("102", "Recent", "recent", "AA:BB:CC:DD:EE:02", "10.0.0.2", "nas", "hotel", "verified", recent_ts, recent_ts))
            conn.commit()
            conn.close()

            result = services.run_retention_cleanup(force=True)
            if result.get("deleted") != 6:
                fail(f"retention cleanup should delete only 6 old rows, got {result}")
            if int(get_setting("retention.days", 0)) != 30:
                fail("retention test setup did not store a low raw value")
            if services.get_retention_config()["days"] != services.MIN_RETENTION_DAYS:
                fail("retention cleanup did not clamp retention days to the legal minimum")

            conn = db_module.db()
            checks = [
                ("pending_auth", "phone"),
                ("call_events", "phone"),
                ("audit_log", "phone"),
                ("radius_accounting", "username"),
                ("guest_sessions", "phone"),
                ("room_auth", "surname_norm"),
            ]
            for table, column in checks:
                old_count = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {column} IN ('79990000181', 'old')"
                ).fetchone()[0]
                recent_count = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {column} IN ('79990000120', 'recent')"
                ).fetchone()[0]
                if old_count != 0 or recent_count != 1:
                    fail(f"retention cleanup mismatch for {table}: old={old_count}, recent={recent_count}")
            conn.close()
        finally:
            db_module.DB_PATH = original_db_path
            settings_store.DB_PATH = original_settings_db_path

    ok("retention cleanup keeps at least 180 days and deletes older personal records")


def check_backup_status() -> None:
    import os
    import sqlite3
    import tempfile
    from datetime import datetime, timezone

    import app_services.settings_store as settings_store
    import db as db_module
    import services
    from app_services.settings_store import init_settings_table, set_setting

    original_db_path = db_module.DB_PATH
    original_settings_db_path = settings_store.DB_PATH

    with tempfile.TemporaryDirectory() as tmp:
        settings_db = Path(tmp) / "settings.db"
        backup_root = Path(tmp) / "backups" / "db"
        db_module.DB_PATH = str(settings_db)
        settings_store.DB_PATH = str(settings_db)

        try:
            init_settings_table()
            set_setting("backup.dest_dir", str(backup_root))
            set_setting("backup.max_age_hours", 36)

            missing = services.get_backup_status()
            if missing["status"] != "warn":
                fail(f"missing manual backup should warn, got {missing}")

            now_dt = datetime.now(timezone.utc)
            backup_dir = backup_root / "hotspot-db-20260526-033000"
            backup_dir.mkdir(parents=True)
            backup_db = backup_dir / "hotspot.db"
            conn = sqlite3.connect(backup_db)
            conn.execute("CREATE TABLE smoke (id INTEGER PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO smoke (value) VALUES ('ok')")
            conn.commit()
            conn.close()
            os.utime(backup_dir, (now_dt.timestamp(), now_dt.timestamp()))
            os.utime(backup_db, (now_dt.timestamp(), now_dt.timestamp()))

            healthy = services.get_backup_status(now_dt=now_dt)
            if healthy["status"] != "ok" or not healthy.get("integrity_ok"):
                fail(f"healthy backup should pass integrity check, got {healthy}")

            broken_dir = backup_root / "hotspot-db-20260526-040000"
            broken_dir.mkdir()
            broken_db = broken_dir / "hotspot.db"
            broken_db.write_text("not sqlite", encoding="utf-8")
            newer_ts = now_dt.timestamp() + 60
            os.utime(broken_dir, (newer_ts, newer_ts))
            os.utime(broken_db, (newer_ts, newer_ts))

            broken = services.get_backup_status(now_dt=now_dt)
            if broken["status"] != "bad" or broken.get("integrity_ok"):
                fail(f"broken backup should fail integrity check, got {broken}")
        finally:
            db_module.DB_PATH = original_db_path
            settings_store.DB_PATH = original_settings_db_path

    ok("manual backup readiness checks freshness and SQLite integrity without restore")


def check_export_jobs() -> None:
    import os
    import subprocess
    import tempfile
    from datetime import datetime, timedelta, timezone
    from zipfile import ZipFile

    import app_services.settings_store as settings_store
    import db as db_module
    from app_services.export_jobs import (
        cleanup_export_job_files,
        create_export_job,
        get_export_job,
        init_export_jobs_table,
        list_export_jobs,
        mark_export_job_done,
        mark_export_job_running,
    )
    from db import init_db
    from exports import write_export_zip_file, write_single_xlsx_file

    original_db_path = db_module.DB_PATH
    original_settings_db_path = settings_store.DB_PATH

    with tempfile.TemporaryDirectory() as tmp:
        test_db = Path(tmp) / "exports.db"
        db_module.DB_PATH = str(test_db)
        settings_store.DB_PATH = str(test_db)

        try:
            init_db()
            init_export_jobs_table()
            now_dt = datetime.now(timezone.utc)
            now = now_dt.isoformat()

            conn = db_module.db()
            conn.execute("""
                INSERT INTO guests (
                    phone, first_verified_at, first_hotel, auth_method,
                    status, created_at, updated_at, last_auth_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "79990000999",
                now,
                "Smoke",
                "call",
                "active",
                now,
                now,
                now,
            ))
            conn.commit()
            conn.close()

            job_id = create_export_job("smoke", "guests", "xlsx")
            mark_export_job_running(job_id)
            mark_export_job_done(job_id, Path(tmp) / "guests.xlsx", "guests.xlsx")
            job = get_export_job(job_id)
            if not job or job["status"] != "done":
                fail("export job state did not move to done")
            if not list_export_jobs(limit=1) or int(list_export_jobs(limit=1)[0]["id"]) != job_id:
                fail("export job list did not return newest job")

            zip_path = Path(tmp) / "export.zip"
            xlsx_path = Path(tmp) / "guests.xlsx"
            write_export_zip_file(zip_path, table_name="all")
            write_single_xlsx_file(xlsx_path, table_name="guests")

            if zip_path.stat().st_size <= 0 or xlsx_path.stat().st_size <= 0:
                fail("export writers produced empty files")

            with ZipFile(zip_path) as zf:
                names = set(zf.namelist())
                if "guests.csv" not in names or "audit_log.csv" not in names:
                    fail("export zip is missing expected CSV files")

            process_job_id = create_export_job("smoke", "guests", "xlsx")
            env = os.environ.copy()
            env["DB_PATH"] = str(test_db)
            env["EXPORT_JOBS_DIR"] = tmp
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            result = subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "tools" / "run_export_job.py"), str(process_job_id)],
                cwd=PROJECT_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                fail(f"export job worker failed: {result.stderr or result.stdout}")

            process_job = get_export_job(process_job_id)
            if not process_job or process_job["status"] != "done":
                fail("export job worker did not mark job as done")
            if not Path(process_job["file_path"]).exists():
                fail("export job worker did not create output file")

            old_file = Path(tmp) / "old_export.xlsx"
            recent_file = Path(tmp) / "recent_export.xlsx"
            old_file.write_text("old export", encoding="utf-8")
            recent_file.write_text("recent export", encoding="utf-8")

            old_job_id = create_export_job("smoke", "guests", "xlsx")
            recent_job_id = create_export_job("smoke", "guests", "xlsx")
            mark_export_job_done(old_job_id, old_file, old_file.name)
            mark_export_job_done(recent_job_id, recent_file, recent_file.name)

            old_dt = now_dt - timedelta(days=8)
            old_ts = old_dt.isoformat()
            conn = db_module.db()
            conn.execute(
                "UPDATE export_jobs SET finished_at = ?, created_at = ? WHERE id = ?",
                (old_ts, old_ts, old_job_id),
            )
            conn.commit()
            conn.close()

            old_log = Path(tmp) / "export_job_999.log"
            old_log.write_text("old log", encoding="utf-8")
            os.utime(old_log, (old_dt.timestamp(), old_dt.timestamp()))

            cleanup_stats = cleanup_export_job_files(Path(tmp), retention_days=7, now_dt=now_dt)
            if cleanup_stats["expired_jobs"] != 1 or cleanup_stats["files_deleted"] != 1:
                fail(f"export cleanup should expire one old export file, got {cleanup_stats}")
            if old_file.exists() or not recent_file.exists() or old_log.exists():
                fail("export cleanup did not remove only old export artifacts")
            if get_export_job(old_job_id)["status"] != "expired":
                fail("export cleanup did not mark old export job as expired")
            if get_export_job(recent_job_id)["status"] != "done":
                fail("export cleanup expired a recent export job")
        finally:
            db_module.DB_PATH = original_db_path
            settings_store.DB_PATH = original_settings_db_path

    ok("export jobs are persisted, run in workers, and old files are cleaned up")


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
    check_admin_role_access_matrix()
    check_opera_lookup_fallback()
    check_optional_api_guard()
    check_internal_api_guards()
    check_pms_api_guard_settings()
    check_legacy_dusit_routes_use_pms_router()
    check_pms_check_result_renderer()
    check_reauth_window_setting()
    check_retention_cleanup()
    check_backup_status()
    check_export_jobs()

    if args.base_url:
        check_http(args.base_url)


if __name__ == "__main__":
    main()
