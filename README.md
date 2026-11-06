# netbox-inventree-sync

A NetBox plugin that synchronizes NetBox inventory data to
[InvenTree](https://inventree.org).

**Status:** scaffold only. The development environment and plugin package are
in place; sync functionality arrives in later tasks.

## Development environment

```bash
./scripts/setup_netbox.sh   # clone NetBox v4.7.4, build netbox/venv, migrate, seed, install plugin
./scripts/start.sh          # start Postgres + Redis (docker), NetBox runserver + rqworker
```

NetBox is then available at <http://localhost:8000> (admin / admin). The
seeded API token is written to `.env` as `NETBOX_TOKEN`.

Requires: Python 3.12 (a uv-managed install works — see `scripts/setup_netbox.sh`),
Docker with compose v2.
