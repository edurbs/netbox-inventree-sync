#!/usr/bin/env bash
# Set up the local NetBox v4.7.4 development environment for plugin work.
#
# Steps (per the plugin tutorial workflow):
#   1. shallow-clone NetBox at tag v4.7.4 into ./netbox
#   2. create netbox/venv on Python 3.12
#   3. pip install -r netbox/requirements.txt
#   4. copy config/configuration.py.tmpl -> netbox/netbox/netbox/configuration.py
#      and config/configuration_test.py.tmpl -> configuration_test.py (test runs)
#   5. docker compose up -d (Postgres + Redis) and wait for Postgres
#   6. manage.py migrate
#   7. scripts/seed_netbox.py  (admin user + API token -> .env)
#   8. pip install -e ..       (this plugin package, editable)
#   9. enable the plugin in configuration.py, then migrate again
#
# Idempotent: safe to re-run; existing clone/venv/configuration are kept.
set -euo pipefail
cd "$(dirname "$0")/.."

NETBOX_DIR="netbox"
VENV_PY="$NETBOX_DIR/venv/bin/python"
VENV_PIP="$NETBOX_DIR/venv/bin/pip"

# --- 1. NetBox sources (shallow clone of the pinned tag) ---------------------
if [[ -d "$NETBOX_DIR/.git" ]]; then
    echo ">> netbox/ clone already present, skipping clone"
else
    git clone --branch v4.7.4 --depth 1 https://github.com/netbox-community/netbox.git netbox
fi

# --- 2. Python 3.12 virtualenv -----------------------------------------------
# Provisioning fallback: prefer a system python3.12; when it is not on PATH,
# fall back to uv, which can download and manage a real CPython 3.12
# (`uv python install 3.12` provisions it -- no root required).
if [[ ! -x "$VENV_PY" ]]; then
    if command -v python3.12 >/dev/null 2>&1; then
        python3.12 -m venv "$NETBOX_DIR/venv"
    else
        echo ">> python3.12 not on PATH, using uv to provide CPython 3.12"
        uv venv --python 3.12 "$NETBOX_DIR/venv"
    fi
fi
# Everything from here on uses the venv interpreters only, never python3.12,
# so how 3.12 was provisioned never leaks into daily commands.
if [[ "$("$VENV_PY" --version)" != "Python 3.12."* ]]; then
    echo "ERROR: $VENV_PY is not Python 3.12 ($("$VENV_PY" --version)); recreate $NETBOX_DIR/venv" >&2
    exit 1
fi

# --- 3. NetBox Python dependencies -------------------------------------------
# Dev-only substitution (controller ruling): swap the source-only psycopg-c
# extra for the prebuilt binary wheel so no libpq-dev headers are needed.
# Same pinned psycopg version, same C extension, delivered as a wheel. This
# file lives inside the git-ignored netbox/ clone, so the repo never carries
# the deviation; the .bak backup preserves the original requirements.
sed -i.bak 's/psycopg\[c,pool\]==3.3.6/psycopg[binary,pool]==3.3.6/' netbox/requirements.txt
"$VENV_PIP" install -r "$NETBOX_DIR/requirements.txt"

# --- 4. Configuration ----------------------------------------------------------
# NetBox expects configuration.py inside its core package, next to
# configuration_example.py (clone/netbox/netbox/configuration.py from the
# clone root).
CONFIG_PY="$NETBOX_DIR/netbox/netbox/configuration.py"
if [[ -f "$CONFIG_PY" ]]; then
    echo ">> $CONFIG_PY already present, keeping it"
else
    cp config/configuration.py.tmpl "$CONFIG_PY"
fi
# Propagate template settings to configurations provisioned before they were
# added (idempotent): DEVELOPER = True is part of the plan-mandated dev config.
if ! grep -q '^DEVELOPER = True' "$CONFIG_PY"; then
    printf "\n# Added by scripts/setup_netbox.sh (missing from older templates)\nDEVELOPER = True\n" \
        >> "$CONFIG_PY"
fi
# Test configuration for canonical plugin test runs (idempotent, kept if
# present): the dev config's DEBUG = True installs the Django debug
# toolbar, which fails every `manage.py test` invocation at the
# debug_toolbar.E001 system check before any test executes. NetBox's own
# convention for tests is a DEBUG-less configuration module (see its
# configuration_testing.py); the template mirrors the live config minus
# DEBUG. Run tests with:
#   NETBOX_CONFIGURATION=netbox.configuration_test \
#       "$VENV_PY" "$NETBOX_DIR/netbox/manage.py" test netbox_inventree_sync
CONFIG_TEST_PY="$NETBOX_DIR/netbox/netbox/configuration_test.py"
if [[ -f "$CONFIG_TEST_PY" ]]; then
    echo ">> $CONFIG_TEST_PY already present, keeping it"
else
    cp config/configuration_test.py.tmpl "$CONFIG_TEST_PY"
fi

# --- 5. Backing services (Postgres + Redis) -----------------------------------
docker compose up -d
echo ">> waiting for PostgreSQL to accept connections"
if ! timeout 90 bash -c 'until docker compose exec -T netbox-db pg_isready -U netbox >/dev/null 2>&1; do sleep 1; done'; then
    echo "ERROR: PostgreSQL not ready after 90s (docker compose logs netbox-db)" >&2
    exit 1
fi

# --- 6. Database migrations ----------------------------------------------------
"$VENV_PY" "$NETBOX_DIR/netbox/manage.py" migrate

# --- 7. Seed admin user + API token (writes NETBOX_TOKEN to .env) --------------
"$VENV_PY" scripts/seed_netbox.py

# --- 8. Install this plugin package (editable) ---------------------------------
# The plugin lives at the repository root, i.e. '..' relative to the netbox/
# clone, matching the tutorial's `pip install -e ..`.
(
    cd "$NETBOX_DIR"
    ./venv/bin/pip install -e ..
)

# --- 9. Enable the plugin, then migrate again (plugin tables) -------------------
# NetBox fails to start if a plugin in PLUGINS is not importable, so the plugin
# is enabled only now, after the package is installed.
if ! grep -q "^PLUGINS = \['netbox_inventree_sync'\]" "$CONFIG_PY"; then
    printf "\nPLUGINS = ['netbox_inventree_sync']\nPLUGINS_CONFIG = {'netbox_inventree_sync': {}}\n" \
        >> "$CONFIG_PY"
fi
"$VENV_PY" "$NETBOX_DIR/netbox/manage.py" migrate

echo ">> setup complete -- start the stack with ./scripts/start.sh"
