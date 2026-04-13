import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

APP_NAME = os.getenv("APP_NAME", "Miracleon Captive Portal")
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "hotspot.db"))

APP_SECRET = os.getenv("APP_SECRET", "change_me")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change_me")
ADMIN_COOKIE = os.getenv("ADMIN_COOKIE", "hotspot_admin")

DEVICE_LIMIT = int(os.getenv("DEVICE_LIMIT", "3"))
PENDING_MINUTES = int(os.getenv("PENDING_MINUTES", "10"))
DEVICE_SYNC_INTERVAL = int(os.getenv("DEVICE_SYNC_INTERVAL", "300"))

MT_HOST = os.getenv("MT_HOST", "192.168.88.1")
MT_PORT = int(os.getenv("MT_PORT", "8728"))
MT_USER = os.getenv("MT_USER", "api-read")
MT_PASS = os.getenv("MT_PASS", "")