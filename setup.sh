#!/usr/bin/env bash
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

echo "=== Hotspot Captive Portal Setup ==="
echo

ASSUME_YES=0
UPGRADE_ONLY=0
RUN_SMOKE=1
INSTALL_RADIUS=1
RADIUS_OPTION_SET=0
PORT=8080
ADMIN_USERNAME_VAL="admin"
ADMIN_PASSWORD_VAL=""
ADMIN_PASSWORD_SOURCE="existing"
RADIUS_CLIENTS_DEFAULT="10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
RADIUS_SECRET_SOURCE="existing"

usage() {
  cat <<EOF
Usage:
  sudo ./setup.sh [options]

Options:
  -y, --yes       Install with generated secrets and sensible defaults.
  --upgrade       Preserve .env and database, update dependencies/services, restart.
  --with-radius   Install or reconfigure FreeRADIUS during --upgrade.
  --skip-radius   Do not install or configure FreeRADIUS.
  --skip-smoke    Do not run post-install smoke checks.
  -h, --help      Show this help.

Notes:
  --upgrade skips FreeRADIUS by default to avoid touching an existing RADIUS setup.
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
    --with-radius)
      INSTALL_RADIUS=1
      RADIUS_OPTION_SET=1
      ;;
    --skip-radius)
      INSTALL_RADIUS=0
      RADIUS_OPTION_SET=1
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

if [ "$UPGRADE_ONLY" = "1" ] && [ "$RADIUS_OPTION_SET" != "1" ]; then
  INSTALL_RADIUS=0
fi

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

generate_radius_secret() {
  python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
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
  local radius_clients
  local radius_secret
  server_host=$(detect_server_host)
  radius_clients=$(read_env_value "RADIUS_CLIENTS" "$RADIUS_CLIENTS_DEFAULT")
  radius_secret=$(read_env_value "RADIUS_SECRET" "")

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

  if [ "$INSTALL_RADIUS" = "1" ]; then
    echo
    echo "FreeRADIUS:"
    echo "  Server: $server_host"
    echo "  Auth port: 1812"
    echo "  Accounting port: 1813"
    echo "  Allowed MikroTik clients: $radius_clients"
    if [ -n "$radius_secret" ]; then
      echo "  Shared secret: $radius_secret"
    fi
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

append_env_value() {
  local key="$1"
  local value="$2"

  escape_env() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
  }

  {
    echo
    echo "$key=\"$(escape_env "$value")\""
  } >> .env
  chmod 600 .env
}

ensure_runtime_env_defaults() {
  if [ "$INSTALL_RADIUS" != "1" ]; then
    return
  fi

  if [ -z "$(read_env_value "RADIUS_SECRET" "")" ]; then
    append_env_value "RADIUS_SECRET" "$(generate_radius_secret)"
    RADIUS_SECRET_SOURCE="generated"
  fi

  if [ -z "$(read_env_value "RADIUS_CLIENTS" "")" ]; then
    append_env_value "RADIUS_CLIENTS" "$RADIUS_CLIENTS_DEFAULT"
  fi
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
  local RADIUS_SECRET_VAL
  local RADIUS_CLIENTS_VAL
  local GENERATED_APP_SECRET
  local GENERATED_ADMIN_PASSWORD
  local GENERATED_RADIUS_SECRET

  GENERATED_APP_SECRET=$(generate_app_secret)
  GENERATED_ADMIN_PASSWORD=$(generate_admin_password)
  GENERATED_RADIUS_SECRET=$(generate_radius_secret)
  VOUCHER_SECRET_KEY_VAL=$(generate_fernet_key)
  OPERA_CACHE_DB_PATH_VAL="$PROJECT_DIR/opera/opera_stays.db"
  RADIUS_SECRET_VAL="$GENERATED_RADIUS_SECRET"
  RADIUS_CLIENTS_VAL="$RADIUS_CLIENTS_DEFAULT"
  RADIUS_SECRET_SOURCE="generated"

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

    if [ "$INSTALL_RADIUS" = "1" ]; then
      RADIUS_SECRET_VAL=$(prompt_secret_generated "FreeRADIUS shared secret" "$RADIUS_SECRET_VAL")
      RADIUS_CLIENTS_VAL=$(prompt_with_default "FreeRADIUS MikroTik client IP/CIDR list" "$RADIUS_CLIENTS_DEFAULT")

      if [ "$RADIUS_SECRET_VAL" = "$GENERATED_RADIUS_SECRET" ]; then
        RADIUS_SECRET_SOURCE="generated"
      else
        RADIUS_SECRET_SOURCE="custom"
      fi
    fi
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
    echo "RADIUS_SECRET=\"$(escape_env "$RADIUS_SECRET_VAL")\""
    echo "RADIUS_CLIENTS=\"$(escape_env "$RADIUS_CLIENTS_VAL")\""

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
  echo "[6/6] Running smoke checks"

  if wait_for_http; then
    .venv/bin/python tools/smoke_check.py --strict-secrets --base-url "http://127.0.0.1:$PORT"
  else
    echo "WARN: portal did not answer on 127.0.0.1:$PORT yet; running offline smoke checks only."
    .venv/bin/python tools/smoke_check.py --strict-secrets
  fi
}

install_freeradius_stack() {
  local fr_dir="/etc/freeradius/3.0"
  local clients_conf="$fr_dir/clients.conf"
  local default_site="$fr_dir/sites-enabled/default"
  local radius_secret
  local radius_clients

  echo
  echo "[5/6] Installing FreeRADIUS"

  if [ "$INSTALL_RADIUS" != "1" ]; then
    echo "Skipping FreeRADIUS install."
    return
  fi

  if [ "$(id -u)" -ne 0 ]; then
    echo "Skipping FreeRADIUS install: run setup.sh as root to install services."
    return
  fi

  if ! command -v apt-get >/dev/null 2>&1; then
    echo "WARN: apt-get not found; skipping FreeRADIUS install."
    return
  fi

  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    -o Dpkg::Options::=--force-confold \
    freeradius freeradius-rest

  if [ ! -d "$fr_dir" ]; then
    die "FreeRADIUS config directory not found: $fr_dir"
  fi

  radius_secret=$(read_env_value "RADIUS_SECRET" "")
  radius_clients=$(read_env_value "RADIUS_CLIENTS" "$RADIUS_CLIENTS_DEFAULT")

  if [ -z "$radius_secret" ]; then
    die "RADIUS_SECRET is empty"
  fi

  if [ ! -f "$clients_conf.hotspot-original" ]; then
    cp "$clients_conf" "$clients_conf.hotspot-original"
  fi

  python3 - "$clients_conf" "$radius_secret" "$radius_clients" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
secret = sys.argv[2]
clients = [item.strip() for item in sys.argv[3].split(",") if item.strip()]
clients = [
    item for item in clients
    if item not in {"127.0.0.1", "127.0.0.1/32", "::1", "localhost"}
]

if not clients:
    raise SystemExit("RADIUS_CLIENTS is empty")

def q(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')

start = "# BEGIN hotspot-captive-portal"
end = "# END hotspot-captive-portal"
text = path.read_text(encoding="utf-8")

while start in text and end in text:
    before, rest = text.split(start, 1)
    _, after = rest.split(end, 1)
    text = before.rstrip() + "\n" + after.lstrip("\n")

block = [start]
for idx, client in enumerate(clients, start=1):
    block.extend([
        f"client hotspot_portal_{idx} {{",
        f"    ipaddr = {client}",
        f"    secret = {q(secret)}",
        f"    shortname = hotspot-portal-{idx}",
        "    nas_type = other",
        "}",
        "",
    ])
block.append(end)

path.write_text(text.rstrip() + "\n\n" + "\n".join(block) + "\n", encoding="utf-8")
PY

  cat > "$fr_dir/mods-available/hotspot_portal_rest" <<EOF
rest hotspot_portal_rest {
    connect_uri = "http://127.0.0.1:$PORT"

    authorize {
        uri = "\${..connect_uri}/radius-check"
        method = "post"
        body = "json"
        data = '{"username":"%{User-Name}","mac":"%{Calling-Station-Id}","ip":"%{Framed-IP-Address}","nas_id":"%{NAS-Identifier}","ssid":"%{Called-Station-Id}","hotel":"%{NAS-Identifier}"}'
    }
}
EOF

  chmod 644 "$fr_dir/mods-available/hotspot_portal_rest"
  if [ -e "$fr_dir/mods-enabled/rest" ]; then
    rm -f "$PROJECT_DIR/backups/freeradius-mods-enabled-rest.hotspot-original"
    cp -a "$fr_dir/mods-enabled/rest" "$PROJECT_DIR/backups/freeradius-mods-enabled-rest.hotspot-original" || true
    rm -f "$fr_dir/mods-enabled/rest"
  fi
  ln -sf ../mods-available/hotspot_portal_rest "$fr_dir/mods-enabled/hotspot_portal_rest"

  chmod 755 "$PROJECT_DIR/scripts/radius_accounting_forward.py"
  cat > "$fr_dir/mods-available/hotspot_accounting_forward" <<EOF
exec hotspot_accounting_forward {
    wait = yes
    input_pairs = request
    shell_escape = yes
    program = "$PROJECT_DIR/scripts/radius_accounting_forward.py '%{%{Acct-Status-Type}:-}' '%{%{Acct-Session-Id}:-}' '%{%{User-Name}:-}' '%{%{Calling-Station-Id}:-}' '%{%{Framed-IP-Address}:-}' '%{%{NAS-IP-Address}:-}' '%{%{NAS-Identifier}:-}' '%{%{NAS-Port-Id}:-}' '%{%{Called-Station-Id}:-}' '%{%{Acct-Terminate-Cause}:-}' '%{%{Acct-Session-Time}:-0}' '%{%{Event-Timestamp}:-}'"
}
EOF

  chmod 644 "$fr_dir/mods-available/hotspot_accounting_forward"
  ln -sf ../mods-available/hotspot_accounting_forward "$fr_dir/mods-enabled/hotspot_accounting_forward"

  if [ -f "$default_site.hotspot-original" ]; then
    mv "$default_site.hotspot-original" "$PROJECT_DIR/backups/freeradius-default-site.hotspot-original"
  fi

  if [ ! -f "$PROJECT_DIR/backups/freeradius-default-site.hotspot-original" ]; then
    cp "$default_site" "$PROJECT_DIR/backups/freeradius-default-site.hotspot-original"
  fi

  python3 - "$default_site" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines()

def section_bounds(name: str):
    start = None
    for idx, line in enumerate(lines):
        if line.strip() == f"{name} {{":
            start = idx
            break
    if start is None:
        raise SystemExit(f"section not found: {name}")

    depth = 0
    for idx in range(start, len(lines)):
        depth += lines[idx].count("{")
        depth -= lines[idx].count("}")
        if idx > start and depth == 0:
            return start, idx
    raise SystemExit(f"section end not found: {name}")

def ensure_in_section(name: str, wanted: str, after_tokens: tuple[str, ...], remove_tokens: tuple[str, ...] = ()):
    global lines
    start, end = section_bounds(name)
    section = lines[start + 1:end]
    section = [
        line for line in section
        if line.strip() not in remove_tokens and line.strip() != wanted
    ]
    if any(line.strip() == wanted for line in section):
        lines = lines[:start + 1] + section + lines[end:]
        return

    insert_at = len(section)
    depth = 0
    for idx, line in enumerate(section):
        stripped = line.strip()
        if depth == 0 and stripped in after_tokens:
            insert_at = idx + 1
            break
        depth += line.count("{")
        depth -= line.count("}")

    section.insert(insert_at, f"\t{wanted}")
    lines = lines[:start + 1] + section + lines[end:]

ensure_in_section(
    "authorize",
    "hotspot_portal_rest",
    after_tokens=("files", "#files", "#\tfiles"),
    remove_tokens=("rest",),
)
ensure_in_section(
    "accounting",
    "hotspot_accounting_forward",
    after_tokens=("unix",),
)

path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

  if ! freeradius -XC >/tmp/hotspot-freeradius-check.log 2>&1; then
    cat /tmp/hotspot-freeradius-check.log
    die "FreeRADIUS config check failed"
  fi

  systemctl enable freeradius.service
  systemctl restart freeradius.service
  echo "FreeRADIUS configured for portal HTTP bridge."
}

if [ "$UPGRADE_ONLY" = "1" ] && [ ! -f ".env" ]; then
  die "--upgrade requires an existing .env. Use --yes for a first install."
fi

check_required_files

echo "[1/6] Preparing virtual environment"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
  echo "Virtual environment created."
else
  echo "Virtual environment already exists."
fi

echo
echo "[2/6] Installing dependencies"
source .venv/bin/activate

if [ ! -x ".venv/bin/python" ]; then
  die "Failed to initialize virtual environment."
fi

pip install --no-cache-dir --upgrade pip --default-timeout=30 --retries 3
pip install --no-cache-dir -r requirements.txt --default-timeout=30 --retries 3

echo
if [ -f ".env" ]; then
  echo "[3/6] Existing .env found; keeping application configuration."
  ADMIN_USERNAME_VAL=$(read_env_value "ADMIN_USERNAME" "admin")
  ADMIN_PASSWORD_SOURCE="existing"
else
  echo "[3/6] Configuring application"
  write_env_file
fi

ensure_runtime_env_defaults
mkdir -p backups docs deploy workers tools opera

echo
echo "[4/6] Installing systemd services"

if [ "$(id -u)" -ne 0 ]; then
  echo "Skipping systemd install: run setup.sh as root to install services."
  echo
  echo "Setup complete without systemd services."
  echo "To run manually:"
  echo "  source .venv/bin/activate"
  echo "  python -m uvicorn app:app --host 0.0.0.0 --port $PORT"

  if [ "$RUN_SMOKE" = "1" ] && [ -f "tools/smoke_check.py" ]; then
    echo
    echo "[6/6] Running offline smoke checks"
    .venv/bin/python tools/smoke_check.py --strict-secrets
  else
    echo
    echo "[6/6] Skipping smoke checks"
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

install_freeradius_stack
run_smoke_checks

echo
echo "Setup complete."
echo
echo "Services:"
systemctl --no-pager --type=service --state=running | grep -E "hotspot-captive-portal|hotspot-cleanup-worker|hotspot-mikrotik-sync-worker" || true
systemctl --no-pager --type=service --state=running | grep -E "freeradius" || true
print_next_steps
