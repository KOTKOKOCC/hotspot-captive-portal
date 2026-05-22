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

prompt_secret_generated() {
  local prompt="$1"
  local generated="$2"
  local value
  read -r -s -p "$prompt [press Enter to generate]: " value
  printf '\n' >&2
  if [ -z "$value" ]; then
    value="$generated"
  fi
  printf '%s' "$value"
}

generate_app_secret() {
  python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
}

generate_admin_password() {
  python3 - <<'PY'
import secrets
import string

alphabet = string.ascii_letters + string.digits
print("".join(secrets.choice(alphabet) for _ in range(18)))
PY
}

generate_fernet_key() {
  python3 - <<'PY'
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode("ascii"))
PY
}

print_next_steps() {
  echo
  echo "Admin panel:"
  echo "  URL: http://SERVER_IP:8080/admin/login"
  echo "  Username: $ADMIN_USERNAME_VAL"

  if [ "$ADMIN_PASSWORD_WAS_GENERATED" = "1" ]; then
    echo "  Password: $ADMIN_PASSWORD_VAL"
  else
    echo "  Password: value entered during setup"
  fi

  echo
  echo "Useful commands:"
  echo "  systemctl status hotspot-captive-portal.service"
  echo "  journalctl -u hotspot-captive-portal.service -f"
}

read_env_value() {
  local key="$1"
  local default="$2"
  local value

  if [ ! -f ".env" ]; then
    printf '%s' "$default"
    return
  fi

  value=$(grep -E "^${key}=" .env | tail -n 1 | cut -d= -f2- || true)
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"

  if [ -z "$value" ]; then
    value="$default"
  fi

  printf '%s' "$value"
}

write_systemd_units() {
  cat > /etc/systemd/system/hotspot-captive-portal.service <<EOF
[Unit]
Description=Hotspot Auth Portal
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$PROJECT_DIR/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$PROJECT_DIR/.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8080 --workers 4 --log-level warning --no-access-log
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  cat > /etc/systemd/system/hotspot-cleanup-worker.service <<EOF
[Unit]
Description=Hotspot Cleanup Worker
After=network.target

[Service]
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$PROJECT_DIR/.env
ExecStart=$PROJECT_DIR/.venv/bin/python $PROJECT_DIR/workers/cleanup_worker.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  cat > /etc/systemd/system/hotspot-mikrotik-sync-worker.service <<EOF
[Unit]
Description=Hotspot MikroTik Sync Worker
After=network.target

[Service]
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$PROJECT_DIR/.env
ExecStart=$PROJECT_DIR/.venv/bin/python $PROJECT_DIR/workers/mikrotik_sync_worker.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  chmod 644 \
    /etc/systemd/system/hotspot-captive-portal.service \
    /etc/systemd/system/hotspot-cleanup-worker.service \
    /etc/systemd/system/hotspot-mikrotik-sync-worker.service

  echo "Installed systemd units for $PROJECT_DIR"
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
ADMIN_USERNAME_VAL="admin"
ADMIN_PASSWORD_WAS_GENERATED=0

if [ -f ".env" ]; then
  echo "[3/4] Existing .env found; keeping application configuration."
  ADMIN_USERNAME_VAL=$(read_env_value "ADMIN_USERNAME" "admin")
else
  echo "[3/4] Configuring application"
  APP_NAME_VAL=$(prompt_with_default "Application name" "Hotspot Captive Portal")
  DB_PATH_VAL=$(prompt_with_default "Database path" "hotspot.db")

  GENERATED_APP_SECRET=$(generate_app_secret)
  GENERATED_ADMIN_PASSWORD=$(generate_admin_password)

  APP_SECRET_VAL=$(prompt_secret_generated "App secret" "$GENERATED_APP_SECRET")
  ADMIN_USERNAME_VAL=$(prompt_with_default "Admin username" "admin")
  ADMIN_PASSWORD_VAL=$(prompt_secret_generated "Admin password" "$GENERATED_ADMIN_PASSWORD")
  ADMIN_COOKIE_VAL=$(prompt_with_default "Admin cookie name" "hotspot_admin")
  VOUCHER_SECRET_KEY_VAL=$(generate_fernet_key)

  if [ "$ADMIN_PASSWORD_VAL" = "$GENERATED_ADMIN_PASSWORD" ]; then
    ADMIN_PASSWORD_WAS_GENERATED=1
  fi

  DEVICE_LIMIT_VAL=$(prompt_with_default "Device limit per phone" "3")
  PENDING_MINUTES_VAL=$(prompt_with_default "Pending auth timeout (minutes)" "10")

  escape_env() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
  }

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
fi

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
  print_next_steps
  exit 0
fi

write_systemd_units
systemctl daemon-reload

systemctl enable --now hotspot-captive-portal.service
systemctl enable --now hotspot-cleanup-worker.service
systemctl enable --now hotspot-mikrotik-sync-worker.service

echo
echo "Setup complete."
echo
echo "Services:"
systemctl --no-pager --type=service --state=running | grep -E "hotspot-captive-portal|hotspot-cleanup-worker|hotspot-mikrotik-sync-worker" || true
print_next_steps
