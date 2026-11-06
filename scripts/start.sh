#!/usr/bin/env bash
# Start the development stack:
#   - Postgres + Redis via docker compose
#   - NetBox dev server (runserver) and RQ worker, both detached
#
# Long-running processes run in the background so this script returns; their
# logs and pid files live under /tmp/opencode/netbox-inventree-sync/.
# It is expected that the processes keep running after the script exits.
#
# Idempotent: re-running is safe; already-running processes are left alone.
set -euo pipefail
cd "$(dirname "$0")/.."

VENV_PY="netbox/venv/bin/python"
LOG_DIR="/tmp/opencode/netbox-inventree-sync"

if [[ ! -x "$VENV_PY" ]]; then
    echo "ERROR: $VENV_PY missing -- run ./scripts/setup_netbox.sh first" >&2
    exit 1
fi

docker compose up -d
mkdir -p "$LOG_DIR"

# start_bg NAME CMD... -- detach CMD unless it is already running (pid file).
start_bg() {
    local name="$1"
    local pidfile="$LOG_DIR/$name.pid"
    shift
    if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        echo ">> $name already running (pid $(cat "$pidfile"))"
        return
    fi
    nohup "$@" >"$LOG_DIR/$name.log" 2>&1 &
    echo $! >"$pidfile"
    echo ">> $name started (pid $!) -- log: $LOG_DIR/$name.log"
}

start_bg runserver "$VENV_PY" netbox/netbox/manage.py runserver 0.0.0.0:8000
start_bg rqworker "$VENV_PY" netbox/netbox/manage.py rqworker high default low

# Bounded readiness wait so callers can rely on NetBox being up on return.
# /login/ serves 200 unauthenticated; the API root requires a token (403).
echo ">> waiting for http://localhost:8000 to respond"
if ! timeout 120 bash -c 'until curl -sf -o /dev/null http://localhost:8000/login/; do sleep 2; done'; then
    echo "ERROR: NetBox did not respond within 120s -- see $LOG_DIR/runserver.log" >&2
    exit 1
fi
echo ">> NetBox is up at http://localhost:8000 (admin/admin)"
