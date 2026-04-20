#!/usr/bin/env bash
set -euo pipefail

echo "[1/8] Stopping custom services and processes..."
systemctl stop hotspot-captive-portal 2>/dev/null || true
systemctl disable hotspot-captive-portal 2>/dev/null || true
systemctl stop freeradius 2>/dev/null || true

pkill -f "uvicorn app:app" 2>/dev/null || true
pkill -f "freeradius -X" 2>/dev/null || true

sleep 1

echo "[2/8] Removing systemd unit..."
rm -f /etc/systemd/system/hotspot-captive-portal.service
systemctl daemon-reload
systemctl reset-failed || true

echo "[3/8] Backing up current FreeRADIUS config..."
BACKUP_DIR="/root/backup-radius-clean"
mkdir -p "$BACKUP_DIR"
if [ -d /etc/freeradius/3.0 ]; then
  rm -rf "$BACKUP_DIR/freeradius-3.0-before-clean"
  cp -a /etc/freeradius/3.0 "$BACKUP_DIR/freeradius-3.0-before-clean"
fi

echo "[4/8] Removing project directories..."
rm -rf /opt/hotspot-captive-portal
rm -rf /opt/hotspot-captive-portal-src
rm -rf /root/hotspot-captive-portal

echo "[5/8] Purging FreeRADIUS packages to reset config..."
apt purge -y freeradius freeradius-rest || true
rm -rf /etc/freeradius/3.0

echo "[6/8] Reinstalling clean FreeRADIUS packages..."
apt update
apt install -y freeradius freeradius-rest

echo "[7/8] Removing residual project logs/databases..."
rm -f /var/log/hotspot-cleanup.log
find / -maxdepth 3 \( -name "hotspot.db" -o -name "hotspot_test.db" \) 2>/dev/null || true

echo "[8/8] Final checks..."
echo
echo "== systemd unit =="
ls -l /etc/systemd/system/hotspot-captive-portal.service 2>/dev/null || echo "hotspot-captive-portal.service not present"

echo
echo "== TCP 8000 listeners =="
ss -ltnp | grep 8000 || echo "nothing listening on 8000"

echo
echo "== UDP 1812/1813 listeners =="
ss -lunp | grep -E '1812|1813' || echo "freeradius not running now"

echo
echo "Cleanup complete."
echo "FreeRADIUS backup saved in: $BACKUP_DIR"