#!/usr/bin/env bash
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_DIR="/opt/hotspot-captive-portal"
SCRIPTS_DIR="$INSTALL_DIR/scripts"

SCRIPT_SRC="$PROJECT_DIR/scripts/radius_accounting_forward.py"
SCRIPT_DST="$SCRIPTS_DIR/radius_accounting_forward.py"
SCRIPT_ENV="$SCRIPTS_DIR/radius_accounting_forward.env"

FR_AVAILABLE="/etc/freeradius/3.0/mods-available/hotspot_accounting_forward"
FR_ENABLED="/etc/freeradius/3.0/mods-enabled/hotspot_accounting_forward"
FR_TEMPLATE="$PROJECT_DIR/deploy/freeradius/hotspot_accounting_forward"

BACKEND_URL="${HOTSPOT_BACKEND_URL:-http://127.0.0.1:8080/radius-accounting}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root."
  exit 1
fi

if ! command -v freeradius >/dev/null 2>&1; then
  echo "ERROR: freeradius is not installed"
  exit 1
fi

if [ ! -d "/etc/freeradius/3.0" ]; then
  echo "ERROR: /etc/freeradius/3.0 not found"
  exit 1
fi

if [ ! -f "$SCRIPT_SRC" ]; then
  echo "ERROR: Script not found: $SCRIPT_SRC"
  exit 1
fi

if [ ! -f "$FR_TEMPLATE" ]; then
  echo "ERROR: FreeRADIUS template not found: $FR_TEMPLATE"
  exit 1
fi

mkdir -p "$SCRIPTS_DIR"

cp "$SCRIPT_SRC" "$SCRIPT_DST"
chmod 755 "$SCRIPT_DST"

cat > "$SCRIPT_ENV" <<EOF
HOTSPOT_BACKEND_URL="$BACKEND_URL"
EOF

cp "$FR_TEMPLATE" "$FR_AVAILABLE"
ln -sf "$FR_AVAILABLE" "$FR_ENABLED"

freeradius -XC

systemctl restart freeradius

echo "FreeRADIUS accounting forward installed."
echo "Backend URL: $BACKEND_URL"