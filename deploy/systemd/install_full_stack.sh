#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_SRC="$PROJECT_DIR/deploy/systemd/hotspot-captive-portal.service"
UNIT_DST="/etc/systemd/system/hotspot-captive-portal.service"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root."
  exit 1
fi

if [ ! -f "$UNIT_SRC" ]; then
  echo "Missing systemd unit file: $UNIT_SRC"
  exit 1
fi

echo "[+] Installing systemd service..."

cp "$UNIT_SRC" "$UNIT_DST"

systemctl daemon-reload
systemctl enable hotspot-captive-portal
systemctl restart hotspot-captive-portal

echo "[+] Checking backend..."

sleep 2

if curl -s http://127.0.0.1:8000/admin/login > /dev/null; then
  echo "[OK] Backend is running"
else
  echo "[ERROR] Backend is NOT responding"
  systemctl status hotspot-captive-portal --no-pager || true
  exit 1
fi
