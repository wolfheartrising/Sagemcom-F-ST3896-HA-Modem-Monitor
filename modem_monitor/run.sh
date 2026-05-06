#!/usr/bin/with-contenv bashio

# Redirect stdout/stderr directly to the container's output fd so output
# bypasses the s6-log pipeline and is visible in the HA add-on log.
exec 1>/proc/1/fd/1 2>/proc/1/fd/2

set -e

echo "=================================="
echo " MODEM MONITOR STARTING (V3.4.2)"
date
echo "=================================="

export PYTHONUNBUFFERED=1

exec python3 /app/modem.py
