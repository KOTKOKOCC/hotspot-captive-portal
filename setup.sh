#!/usr/bin/env bash
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

echo "=== Hotspot Captive Portal Setup ==="
echo

ASSUME_YES=0
UPGRADE_ONLY=0
RUN_SMOKE=1
PORT=8080
ADMIN_USERNAME_VAL="admin"
ADMIN_PASSWORD_VAL=""
ADMIN_PASSWORD_SOURCE="existing"

usage() {
  cat <<EOF
Usage:
  sudo ./setup.sh [options]

Options:
  -y, --yes       Install with generated secrets and sensible defaults.
  --upgrade       Preserve .env and database, update dependencies/services, restart.
  --skip-smoke    Do not run post-install smoke checks.
  -h, --help      Show this help.
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    -y|--yes|--non-interactive)
      ASSUME_YES=1
      ;;
    --upgrade)
      UPGRADE_ONLY=1
      ASSUME_YES=1
      ;;
    --skip-smoke)
      RUN_SMOKE=0
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown option: $1"
      ;;
  esac
  shift
done

if ! command -v python3 >/dev/null 2>&1; then
  die "python3 not found. Install Python 3.10+ first."
fi

if ! python3 -m venv --help >/dev/null 2>&1; then
  die "python3 venv module is not available. Install python3-venv first."
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
import base64
import os
print(base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"))
PY
}

detect_server_host() {
  local host
  host=$(hostname -I 2>/dev/null | awk '{print $1}' || true)

  if [ -z "$host" ]; then
    host=$(hostname 2>/dev/null || true)
  fi

  if [ -z "$host" ]; then
    host="SERVER_IP"
  fi

  printf '%s' "$host"
}

print_next_steps() {
  local server_host
  server_host=$(detect_server_host)

  echo
  echo "Admin panel:"
  echo "  URL: http://$server_host:$PORT/admin/login"
  echo "  Username: $ADMIN_USERNAME_VAL"

  if [ "$ADMIN_PASSWORD_SOURCE" = "generated" ]; then
    echo "  Password: $ADMIN_PASSWORD_VAL"
  elif [ "$ADMIN_PASSWORD_SOURCE" = "custom" ]; then
    echo "  Password: value entered during setup"
  else
    echo "  Password: existing admin password"
  fi

  echo
  echo "Next:"
  echo "  Open the admin UI and configure MikroTik, Networks, 1C/Opera, and PMS checks."
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

check_required_files() {
  local missing=""

  for path in app.py requirements.txt admin_auth.py db.py tools/smoke_check.py workers/cleanup_worker.py workers/mikrotik_sync_worker.py; do
    if [ ! -e "$path" ]; then
      missing="$missing $path"
    fi
  done

  if [ -n "$missing" ]; then
    die "Missing required project files:$missing"
  fi
}

check_port_warning() {
  if ! command -v ss >/dev/null 2>&1; then
    return
  fi

  if ss -ltn | awk '{print $4}' | grep -Eq "[:.]$PORT$"; then
    if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet hotspot-captive-portal.service 2>/dev/null; then
      echo "Port $PORT is already used by hotspot-captive-portal.service; continuing."
    else
      echo "WARN: port $PORT is already listening. The portal service may fail to start."
    fi
  fi
}

write_env_file() {
  local APP_NAME_VAL
  local DB_PATH_VAL
  local APP_SECRET_VAL
  local ADMIN_COOKIE_VAL
  local VOUCHER_SECRET_KEY_VAL
  local DEVICE_LIMIT_VAL
  local PENDING_MINUTES_VAL
  local OPERA_CACHE_DB_PATH_VAL
  local GENERATED_APP_SECRET
  local GENERATED_ADMIN_PASSWORD

  GENERATED_APP_SECRET=$(generate_app_secret)
  GENERATED_ADMIN_PASSWORD=$(generate_admin_password)
  VOUCHER_SECRET_KEY_VAL=$(generate_fernet_key)
  OPERA_CACHE_DB_PATH_VAL="$PROJECT_DIR/opera/opera_stays.db"

  if [ "$ASSUME_YES" = "1" ]; then
    APP_NAME_VAL="Hotspot Captive Portal"
    DB_PATH_VAL="hotspot.db"
    APP_SECRET_VAL="$GENERATED_APP_SECRET"
    ADMIN_USERNAME_VAL="admin"
    ADMIN_PASSWORD_VAL="$GENERATED_ADMIN_PASSWORD"
    ADMIN_COOKIE_VAL="hotspot_admin"
    DEVICE_LIMIT_VAL="3"
    PENDING_MINUTES_VAL="10"
    ADMIN_PASSWORD_SOURCE="generated"
  else
    APP_NAME_VAL=$(prompt_with_default "Application name" "Hotspot Captive Portal")
    DB_PATH_VAL=$(prompt_with_default "Database path" "hotspot.db")

    APP_SECRET_VAL=$(prompt_secret_generated "App secret" "$GENERATED_APP_SECRET")
    ADMIN_USERNAME_VAL=$(prompt_with_default "Admin username" "admin")
    ADMIN_PASSWORD_VAL=$(prompt_secret_generated "Admin password" "$GENERATED_ADMIN_PASSWORD")
    ADMIN_COOKIE_VAL=$(prompt_with_default "Admin cookie name" "hotspot_admin")

    if [ "$ADMIN_PASSWORD_VAL" = "$GENERATED_ADMIN_PASSWORD" ]; then
      ADMIN_PASSWORD_SOURCE="generated"
    else
      ADMIN_PASSWORD_SOURCE="custom"
    fi

    DEVICE_LIMIT_VAL=$(prompt_with_default "Device limit per phone" "3")
    PENDING_MINUTES_VAL=$(prompt_with_default "Pending auth timeout (minutes)" "10")
  fi

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
    echo "OPERA_CACHE_DB_PATH=\"$(escape_env "$OPERA_CACHE_DB_PATH_VAL")\""

    echo "DEVICE_LIMIT=\"$(escape_env "$DEVICE_LIMIT_VAL")\""
    echo "PENDING_MINUTES=\"$(escape_env "$PENDING_MINUTES_VAL")\""
  } > .env

  chmod 600 .env
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

wait_for_http() {
  local url="http://127.0.0.1:$PORT/admin/login"
  local attempt

  for attempt in $(seq 1 20); do
    if .venv/bin/python - "$url" <<'PY' >/dev/null 2>&1
import sys
import urllib.request

url = sys.argv[1]
try:
    with urllib.request.urlopen(url, timeout=2) as response:
        raise SystemExit(0 if response.status == 200 else 1)
except Exception:
    raise SystemExit(1)
PY
    then
      return 0
    fi

    sleep 1
  done

  return 1
}

run_smoke_checks() {
  if [ "$RUN_SMOKE" != "1" ]; then
    echo "Skipping smoke checks."
    return
  fi

  if [ ! -f "tools/smoke_check.py" ]; then
    echo "Skipping smoke checks: tools/smoke_check.py not found."
    return
  fi

  echo
  echo "[5/5] Running smoke checks"

  if wait_for_http; then
    .venv/bin/python tools/smoke_check.py --strict-secrets --base-url "http://127.0.0.1:$PORT"
  else
    echo "WARN: portal did not answer on 127.0.0.1:$PORT yet; running offline smoke checks only."
    .venv/bin/python tools/smoke_check.py --strict-secrets
  fi
}

if [ "$UPGRADE_ONLY" = "1" ] && [ ! -f ".env" ]; then
  die "--upgrade requires an existing .env. Use --yes for a first install."
fi

check_required_files

echo "[1/5] Preparing virtual environment"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
  echo "Virtual environment created."
else
  echo "Virtual environment already exists."
fi

echo
echo "[2/5] Installing dependencies"
source .venv/bin/activate

if [ ! -x ".venv/bin/python" ]; then
  die "Failed to initialize virtual environment."
fi

pip install --no-cache-dir --upgrade pip --default-timeout=30 --retries 3
pip install --no-cache-dir -r requirements.txt --default-timeout=30 --retries 3

echo
if [ -f ".env" ]; then
  echo "[3/5] Existing .env found; keeping application configuration."
  ADMIN_USERNAME_VAL=$(read_env_value "ADMIN_USERNAME" "admin")
  ADMIN_PASSWORD_SOURCE="existing"
else
  echo "[3/5] Configuring application"
  write_env_file
fi

mkdir -p backups docs deploy workers tools opera

echo
echo "[4/5] Installing systemd services"

if [ "$(id -u)" -ne 0 ]; then
  echo "Skipping systemd install: run setup.sh as root to install services."
  echo
  echo "Setup complete without systemd services."
  echo "To run manually:"
  echo "  source .venv/bin/activate"
  echo "  python -m uvicorn app:app --host 0.0.0.0 --port $PORT"

  if [ "$RUN_SMOKE" = "1" ] && [ -f "tools/smoke_check.py" ]; then
    echo
    echo "[5/5] Running offline smoke checks"
    .venv/bin/python tools/smoke_check.py --strict-secrets
  else
    echo
    echo "[5/5] Skipping smoke checks"
  fi
  print_next_steps
  exit 0
fi

check_port_warning
write_systemd_units
systemctl daemon-reload

systemctl enable hotspot-captive-portal.service
systemctl enable hotspot-cleanup-worker.service
systemctl enable hotspot-mikrotik-sync-worker.service

systemctl restart hotspot-captive-portal.service
systemctl restart hotspot-cleanup-worker.service
systemctl restart hotspot-mikrotik-sync-worker.service

run_smoke_checks

echo
echo "Setup complete."
echo
echo "Services:"
systemctl --no-pager --type=service --state=running | grep -E "hotspot-captive-portal|hotspot-cleanup-worker|hotspot-mikrotik-sync-worker" || true
print_next_steps
