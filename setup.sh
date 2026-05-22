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
  printf '%s' "$value"
}

prompt_secret_with_default() {
  local prompt="$1"
  local default="$2"
  local value
  read -r -s -p "$prompt [$default]: " value
  printf '\n' >&2
  if [ -z "$value" ]; then
    value="$default"
  fi
  printf '%s' "$value"
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
VOUCHER_SECRET_KEY_VAL=$(python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
)

DEVICE_LIMIT_VAL=$(prompt_with_default "Device limit per phone" "3")
PENDING_MINUTES_VAL=$(prompt_with_default "Pending auth timeout (minutes)" "10")




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

  echo "VOUCHER_SECRET_KEY=\"$(escape_env "$VOUCHER_SECRET_KEY_VAL")\""

  echo "DEVICE_LIMIT=\"$(escape_env "$DEVICE_LIMIT_VAL")\""
  echo "PENDING_MINUTES=\"$(escape_env "$PENDING_MINUTES_VAL")\""

} > .env

mkdir -p backups docs deploy workers

echo
echo "[4/4] Installing systemd services"

if [ "$(id -u)" -ne 0 ]; then
  echo "Skipping systemd install: run setup.sh as root to install services."
  echo
  echo "Setup complete without systemd services."
  echo "To run manually:"
  echo "  source .venv/bin/activate"
  echo "  python -m uvicorn app:app --host 0.0.0.0 --port 8080"
  exit 0
fi

install_service() {
  local src="$1"
  local dst="$2"

  if [ ! -f "$src" ]; then
    echo "Missing service file: $src"
    exit 1
  fi

  cp "$src" "$dst"
  chmod 644 "$dst"
  echo "Installed $dst"
}

install_service "deploy/hotspot-captive-portal.service" "/etc/systemd/system/hotspot-captive-portal.service"
install_service "deploy/hotspot-cleanup-worker.service" "/etc/systemd/system/hotspot-cleanup-worker.service"
install_service "deploy/hotspot-mikrotik-sync-worker.service" "/etc/systemd/system/hotspot-mikrotik-sync-worker.service"

systemctl daemon-reload

systemctl enable --now hotspot-captive-portal.service
systemctl enable --now hotspot-cleanup-worker.service
systemctl enable --now hotspot-mikrotik-sync-worker.service

echo
echo "Setup complete."
echo
echo "Services:"
systemctl --no-pager --type=service --state=running | grep -E "hotspot-captive-portal|hotspot-cleanup-worker|hotspot-mikrotik-sync-worker" || true