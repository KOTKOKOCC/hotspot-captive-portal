{\rtf1\ansi\ansicpg1251\cocoartf2869
\cocoatextscaling0\cocoaplatform0{\fonttbl\f0\fswiss\fcharset0 Helvetica;}
{\colortbl;\red255\green255\blue255;}
{\*\expandedcolortbl;;}
\paperw11900\paperh16840\margl1440\margr1440\vieww11520\viewh8400\viewkind0
\pard\tx720\tx1440\tx2160\tx2880\tx3600\tx4320\tx5040\tx5760\tx6480\tx7200\tx7920\tx8640\pardirnatural\partightenfactor0

\f0\fs24 \cf0 #!/usr/bin/env bash\
set -e\
\
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"\
cd "$PROJECT_DIR"\
\
echo "=== Hotspot Captive Portal Setup ==="\
echo\
\
if ! command -v python3 >/dev/null 2>&1; then\
  echo "python3 not found. Install Python 3.10+ first."\
  exit 1\
fi\
\
prompt_with_default() \{\
  local prompt="$1"\
  local default="$2"\
  local value\
  read -r -p "$prompt [$default]: " value\
  if [ -z "$value" ]; then\
    value="$default"\
  fi\
  echo "$value"\
\}\
\
prompt_secret_with_default() \{\
  local prompt="$1"\
  local default="$2"\
  local value\
  read -r -s -p "$prompt [$default]: " value\
  echo\
  if [ -z "$value" ]; then\
    value="$default"\
  fi\
  echo "$value"\
\}\
\
echo "[1/4] Preparing virtual environment"\
if [ ! -d ".venv" ]; then\
  python3 -m venv .venv\
  echo "Virtual environment created."\
else\
  echo "Virtual environment already exists."\
fi\
\
echo\
echo "[2/4] Installing dependencies"\
source .venv/bin/activate\
pip install --upgrade pip\
pip install -r requirements.txt\
\
echo\
echo "[3/4] Configuring application"\
APP_NAME_VAL=$(prompt_with_default "Application name" "Hotspot Captive Portal")\
DB_PATH_VAL=$(prompt_with_default "Database path" "hotspot.db")\
\
APP_SECRET_VAL=$(prompt_secret_with_default "App secret" "change_me")\
ADMIN_USERNAME_VAL=$(prompt_with_default "Admin username" "admin")\
ADMIN_PASSWORD_VAL=$(prompt_secret_with_default "Admin password" "change_me")\
ADMIN_COOKIE_VAL=$(prompt_with_default "Admin cookie name" "hotspot_admin")\
\
DEVICE_LIMIT_VAL=$(prompt_with_default "Device limit per phone" "3")\
PENDING_MINUTES_VAL=$(prompt_with_default "Pending auth timeout (minutes)" "10")\
DEVICE_SYNC_INTERVAL_VAL=$(prompt_with_default "MikroTik sync interval (seconds)" "300")\
\
MT_HOST_VAL=$(prompt_with_default "MikroTik host" "192.168.88.1")\
MT_PORT_VAL=$(prompt_with_default "MikroTik API port" "8728")\
MT_USER_VAL=$(prompt_with_default "MikroTik API user" "api-read")\
MT_PASS_VAL=$(prompt_secret_with_default "MikroTik API password" "change_me")\
\
cat > .env <<EOF\
APP_NAME=$APP_NAME_VAL\
DB_PATH=$DB_PATH_VAL\
\
APP_SECRET=$APP_SECRET_VAL\
ADMIN_USERNAME=$ADMIN_USERNAME_VAL\
ADMIN_PASSWORD=$ADMIN_PASSWORD_VAL\
ADMIN_COOKIE=$ADMIN_COOKIE_VAL\
\
DEVICE_LIMIT=$DEVICE_LIMIT_VAL\
PENDING_MINUTES=$PENDING_MINUTES_VAL\
DEVICE_SYNC_INTERVAL=$DEVICE_SYNC_INTERVAL_VAL\
\
MT_HOST=$MT_HOST_VAL\
MT_PORT=$MT_PORT_VAL\
MT_USER=$MT_USER_VAL\
MT_PASS=$MT_PASS_VAL\
EOF\
\
mkdir -p backups docs deploy\
\
echo\
echo "[4/4] Setup complete"\
echo ".env created successfully."\
echo\
\
read -r -p "Start backend now? [y/N]: " START_NOW\
if [[ "$START_NOW" =~ ^[Yy]$ ]]; then\
  exec python -m uvicorn app:app --host 0.0.0.0 --port 8000\
fi\
\
echo "Done."\
echo "To start later:"\
echo "  source .venv/bin/activate"\
echo "  python -m uvicorn app:app --host 0.0.0.0 --port 8000"}