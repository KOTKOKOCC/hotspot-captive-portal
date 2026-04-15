#!/usr/bin/env bash
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

echo "=== Hotspot Captive Portal Setup ==="
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found. Install Python 3.10+ first."
  exit 1
fi

prompt_with_default() {
  local prompt="$1"
  local default="$2"
  local value
  read -r -p "$prompt [$default]: " value
  if [ -z "$value" ]; then
    value="$default"
  fi
  echo "$value"
}

prompt_secret_with_default() {
  local prompt="$1"
  local default="$2"
  local value
  read -r -s -p "$prompt [$default]: " value
  echo
  if [ -z "$value" ]; then
    value="$default"
  fi
  echo "$value"
}

echo "[1/4] Preparing virtual environment"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
  echo "Virtual environment created."
else
  echo "Virtual environment already exists."
fi

echo
echo "[2/4] Installing dependencies"
source .venv/bin/activate

if [ ! -x ".venv/bin/python" ]; then
  echo "Failed to initialize virtual environment."
  exit 1
fi

pip install --upgrade pip --default-timeout=100 --retries 10
pip install -r requirements.txt --default-timeout=100 --retries 10

echo
echo "[3/4] Configuring application"
APP_NAME_VAL=$(prompt_with_default "Application name" "Hotspot Captive Portal")
DB_PATH_VAL=$(prompt_with_default "Database path" "hotspot.db")

APP_SECRET_VAL=$(prompt_secret_with_default "App secret" "change_me")
ADMIN_USERNAME_VAL=$(prompt_with_default "Admin username" "admin")
ADMIN_PASSWORD_VAL=$(prompt_secret_with_default "Admin password" "change_me")
ADMIN_COOKIE_VAL=$(prompt_with_default "Admin cookie name" "hotspot_admin")

DEVICE_LIMIT_VAL=$(prompt_with_default "Device limit per phone" "3")
PENDING_MINUTES_VAL=$(prompt_with_default "Pending auth timeout (minutes)" "10")
DEVICE_SYNC_INTERVAL_VAL=$(prompt_with_default "MikroTik sync interval (seconds)" "300")

MT_HOST_VAL=$(prompt_with_default "MikroTik host" "192.168.88.1")
MT_PORT_VAL=$(prompt_with_default "MikroTik API port" "8728")
if ! [[ "$MT_PORT_VAL" =~ ^[0-9]+$ ]]; then
  echo "ERROR: MikroTik API port must be a number."
  exit 1
fi
MT_USER_VAL=$(prompt_with_default "MikroTik API user" "api-read")
MT_PASS_VAL=$(prompt_secret_with_default "MikroTik API password" "change_me")

escape_env() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

rm -f .env

{
  echo "APP_NAME=\"$(escape_env "$APP_NAME_VAL")\""
  echo "DB_PATH=\"$(escape_env "$DB_PATH_VAL")\""

  echo "APP_SECRET=\"$(escape_env "$APP_SECRET_VAL")\""
  echo "ADMIN_USERNAME=\"$(escape_env "$ADMIN_USERNAME_VAL")\""
  echo "ADMIN_PASSWORD=\"$(escape_env "$ADMIN_PASSWORD_VAL")\""
  echo "ADMIN_COOKIE=\"$(escape_env "$ADMIN_COOKIE_VAL")\""

  echo "DEVICE_LIMIT=\"$(escape_env "$DEVICE_LIMIT_VAL")\""
  echo "PENDING_MINUTES=\"$(escape_env "$PENDING_MINUTES_VAL")\""
  echo "DEVICE_SYNC_INTERVAL=\"$(escape_env "$DEVICE_SYNC_INTERVAL_VAL")\""

  echo "MT_HOST=\"$(escape_env "$MT_HOST_VAL")\""
  echo "MT_PORT=\"$(escape_env "$MT_PORT_VAL")\""
  echo "MT_USER=\"$(escape_env "$MT_USER_VAL")\""
  echo "MT_PASS=\"$(escape_env "$MT_PASS_VAL")\""
} > .env

mkdir -p backups docs deploy

echo
echo "[4/4] Setup complete"
echo ".env created successfully."
echo

read -r -p "Start backend now? [y/N]: " START_NOW
START_NOW="$(printf '%s' "$START_NOW" | tr '[:upper:]' '[:lower:]')"

if [[ "$START_NOW" == "y" || "$START_NOW" == "yes" || "$START_NOW" == "да" ]]; then
  exec python -m uvicorn app:app --host 0.0.0.0 --port 8000
fi

echo "Done."
echo "To start later:"
echo "  source .venv/bin/activate"
echo "  python -m uvicorn app:app --host 0.0.0.0 --port 8000"