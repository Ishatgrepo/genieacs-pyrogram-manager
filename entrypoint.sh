#!/bin/sh
set -eu
# One ASGI process owns the bot. Core workers coordinate through MongoDB leases.
exec uvicorn app.main:app --host 0.0.0.0 --port "${APP_PORT:-7860}" --workers 1 --no-access-log
