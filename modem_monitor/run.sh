#!/usr/bin/with-contenv bashio

set -e

echo "=================================="
echo " MODEM MONITOR STARTING (V3.4.3)"
echo " $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "=================================="

export PYTHONUNBUFFERED=1

exec python3 /app/modem.py

