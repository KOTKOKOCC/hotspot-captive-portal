#!/usr/bin/env bash
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT_SRC="$PROJECT_DIR/scripts/radius_accounting_forward.py"
SCRIPT_DST="/opt/hotspot-captive-portal/scripts/radius_accounting_forward.py"

FR_AVAILABLE="/etc/freeradius/3.0/mods-available/hotspot_accounting_forward"
FR_ENABLED="/etc/freeradius/3.0/mods-enabled/hotspot_accounting_forward"
FR_TEMPLATE="$PROJECT_DIR/deploy/freeradius/hotspot_accounting_forward"

BACKEND_URL="${HOTSPOT_BACKEND_URL:-http://127.0.0.1:8080/radius-accounting}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root."
  exit 1
fi

mkdir -p /opt/hotspot-captive-portal/scripts

cp "$SCRIPT_SRC" "$SCRIPT_DST"
chmod 755 "$SCRIPT_DST"

cat > /opt/hotspot-captive-portal/scripts/radius_accounting_forward.env <<EOF
HOTSPOT_BACKEND_URL="$BACKEND_URL"
EOF

cp "$FR_TEMPLATE" "$FR_AVAILABLE"
ln -sf "$FR_AVAILABLE" "$FR_ENABLED"

if command -v freeradius >/dev/null 2>&1; then
  freeradius -XC
fi

systemctl restart freeradius

echo "FreeRADIUS accounting forward installed."
echo "Backend URL: $BACKEND_URL"