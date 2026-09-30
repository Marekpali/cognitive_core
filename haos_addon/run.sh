#!/bin/sh
set -eu

echo "[COGNITIVE_CORE] Starting as Home Assistant App"
mkdir -p /data

if [ ! -f /data/core.db ] && [ -f /share/cognitive_core_backup/core.db.step2 ]; then
    echo "[COGNITIVE_CORE] Importing STEP 2 database"
    cp /share/cognitive_core_backup/core.db.step2 /data/core.db
fi

export DB_PATH="/data/core.db"
export HA_WS_URL="ws://supervisor/core/websocket"

if [ -z "${SUPERVISOR_TOKEN:-}" ]; then
    echo "[COGNITIVE_CORE] ERROR: SUPERVISOR_TOKEN missing"
    exit 1
fi

echo "[COGNITIVE_CORE] Database: ${DB_PATH}"
echo "[COGNITIVE_CORE] Home Assistant proxy configured"

cd /app
exec python -m src.main
