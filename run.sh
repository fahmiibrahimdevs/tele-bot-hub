#!/usr/bin/env bash
cd "$(dirname "$0")"

export TZ="Asia/Jakarta"

# Load environment variables if .env exists
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

if [ ! -d ".venv" ]; then
    echo "Virtual environment .venv tidak ditemukan. Menyiapkan venv..."
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -r requirements.txt
fi

PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"

echo "======================================================"
echo "🚀 Memulai Telegram Bot Hub Portal..."
echo "🌐 URL Web Dashboard: http://localhost:${PORT}"
echo "👤 Akun Login      : Silakan login dengan akun admin Anda"
echo "======================================================"

exec .venv/bin/uvicorn app.main:app --host "${HOST}" --port "${PORT}"
