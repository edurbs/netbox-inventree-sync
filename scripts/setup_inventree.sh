#!/usr/bin/env bash
# Set up the InvenTree 1.7.0 side of the development stack.
#
# Steps:
#   1. docker compose up -d inventree-db inventree-redis, wait until healthy
#   2. docker compose run --rm inventree-web invoke update
#      (documented image init: install -> backup -> migrate -> static)
#   3. docker compose up -d inventree-web inventree-worker
#   4. wait for the API health endpoint on http://localhost:1337
#   5. seed admin user + API token by piping scripts/seed_inventree.py into
#      the container's Django shell, and store the printed key as
#      INVENTREE_TOKEN in .env (append/update, like seed_netbox.py does for
#      NETBOX_TOKEN)
#
# Idempotent: safe to re-run; docker volumes keep the database state, the
# seed re-uses the existing admin user and token (same INVENTREE_TOKEN value).
set -euo pipefail
cd "$(dirname "$0")/.."

BASE_URL="http://localhost:1337"
ENV_FILE=".env"
SEED_OUTPUT="$(mktemp /tmp/opencode/inventree-seed.XXXXXX)"
trap 'rm -f "$SEED_OUTPUT"' EXIT

# --- 1. Backing services (Postgres + Redis) ------------------------------------
docker compose up -d inventree-db inventree-redis

echo ">> waiting for inventree-db to accept connections"
if ! timeout 90 bash -c 'until docker compose exec -T inventree-db sh -c "pg_isready -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\"" >/dev/null 2>&1; do sleep 1; done'; then
    echo "ERROR: inventree-db not ready after 90s (docker compose logs inventree-db)" >&2
    exit 1
fi

echo ">> waiting for inventree-redis to answer PING"
if ! timeout 60 bash -c 'until [[ "$(docker compose exec -T inventree-redis redis-cli ping 2>/dev/null)" == "PONG" ]]; do sleep 1; done'; then
    echo "ERROR: inventree-redis not ready after 60s (docker compose logs inventree-redis)" >&2
    exit 1
fi

# --- 2. One-time container init (migrations + static files) --------------------
# Documented image init path; INVENTREE_DOCKER makes it skip frontend work.
docker compose run --rm inventree-web invoke update

# --- 3. Long-running services ---------------------------------------------------
docker compose up -d inventree-web inventree-worker

# --- 4. Bounded readiness wait --------------------------------------------------
# /api/system/health/ is the same unauthenticated endpoint the image's own
# healthcheck (invoke server-health) polls; 200 means gunicorn is serving.
echo ">> waiting for $BASE_URL to respond"
if ! timeout 180 bash -c "until curl -sf -o /dev/null '$BASE_URL/api/system/health/'; do sleep 2; done"; then
    echo "ERROR: InvenTree did not respond within 180s (docker compose logs inventree-web)" >&2
    exit 1
fi

# --- 5. Seed admin user + API token -> INVENTREE_TOKEN in .env ------------------
# The 1.7.0 image does not ship an `inventree-shell` entry point, so the seed
# script is piped into the image's Django management shell instead (the
# entrypoint has already configured INVENTREE_* -> Django settings).
echo ">> seeding admin user and API token"
docker compose exec -T -w /home/inventree/src/backend/InvenTree \
    inventree-web python3 manage.py shell <scripts/seed_inventree.py | tee "$SEED_OUTPUT"

TOKEN="$(grep -E '^INVENTREE_TOKEN=' "$SEED_OUTPUT" | tail -1 | cut -d= -f2- || true)"
if [[ -z "$TOKEN" ]]; then
    echo "ERROR: seed completed but no INVENTREE_TOKEN= line was printed -- see output above" >&2
    exit 1
fi

if grep -q '^INVENTREE_TOKEN=' "$ENV_FILE"; then
    sed -i "s|^INVENTREE_TOKEN=.*$|INVENTREE_TOKEN=${TOKEN}|" "$ENV_FILE"
else
    printf '\n# InvenTree API token of the seeded admin user; written by scripts/setup_inventree.sh.\nINVENTREE_TOKEN=%s\n' "$TOKEN" >>"$ENV_FILE"
fi
echo ">> INVENTREE_TOKEN written to $ENV_FILE"

echo ">> setup complete -- InvenTree is at $BASE_URL (admin/admin)"
echo ">> smoke test: netbox/venv/bin/python scripts/smoke_inventree.py"
