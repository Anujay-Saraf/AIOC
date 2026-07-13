#!/bin/sh
set -eu

: "${PORT:=8000}"
: "${WEB_PORT:=3000}"

cd /app/web
npm run start -- --hostname 127.0.0.1 --port "${WEB_PORT}" >/tmp/incident-web.log 2>&1 &
WEB_PID=$!

cleanup() {
  kill "${WEB_PID}" >/dev/null 2>&1 || true
}

trap cleanup INT TERM EXIT

WEB_PORT="${WEB_PORT}" python - <<'PY'
import os
import socket
import time

web_port = int(os.environ["WEB_PORT"])
for _ in range(120):
    try:
        with socket.create_connection(("127.0.0.1", web_port), timeout=1):
            break
    except OSError:
        time.sleep(0.5)
else:
    raise SystemExit("Next.js frontend failed to start")
PY

cd /app
exec python -m uvicorn app:app --host 0.0.0.0 --port "${PORT}"
