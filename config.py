import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

if ENV_FILE.exists():
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)

PBX_ALLOWED_IPS = [
    ip.strip()
    for ip in os.getenv("PBX_ALLOWED_IPS", "").split(",")
    if ip.strip()
]

APP_NAME = os.getenv("APP_NAME", "C-Portal")
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "hotspot.db"))
OPERA_CACHE_DB_PATH = os.getenv(
    "OPERA_CACHE_DB_PATH",
    "/opt/hotspot-captive-portal/opera/opera_stays.db"
)


APP_SECRET = os.getenv("APP_SECRET", "change_me")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change_me")
ADMIN_COOKIE = os.getenv("ADMIN_COOKIE", "hotspot_admin")


DEVICE_LIMIT = int(os.getenv("DEVICE_LIMIT", "5"))
PENDING_MINUTES = int(os.getenv("PENDING_MINUTES", "10"))
