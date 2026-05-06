#!/bin/bash

echo "=================================="
echo " MODEM MONITOR STARTING (V3.4.4)"
echo " $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "=================================="

export PYTHONUNBUFFERED=1

exec python3 -u /app/modem.py

