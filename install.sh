#!/usr/bin/env bash
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

echo "[1/5] Creating virtual environment"
python3 -m venv .venv

echo "[2/5] Activating virtual environment"
source .venv/bin/activate

echo "[3/5] Installing dependencies"
pip install --upgrade pip
pip install -r requirements.txt

echo "[4/5] Preparing .env"
if [ ! -f .env ]; then
  cp .env.example .env
  echo ".env created from .env.example"
else
  echo ".env already exists, keeping current file"
fi

echo "[5/5] Creating folders"
mkdir -p backups docs

echo
echo "Done."
echo "Next:"
echo "  1. Edit .env"
echo "  2. Run: source .venv/bin/activate"
echo "  3. Run: python -m uvicorn app:app --host 0.0.0.0 --port 8000"