#!/usr/bin/with-contenv bashio

set -e

echo "=================================="
echo " MODEM MONITOR STARTING (V3.2.0)"
date
echo "=================================="

export PYTHONUNBUFFERED=1

exec python3 /app/modem.py
