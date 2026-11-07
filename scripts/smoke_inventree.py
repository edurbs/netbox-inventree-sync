#!/usr/bin/env python3
"""Smoke-test the local InvenTree dev instance from the host.

    netbox/venv/bin/python scripts/smoke_inventree.py

Asserts, against http://localhost:1337 with the INVENTREE_TOKEN seeded into
``.env`` by ``scripts/setup_inventree.sh``:

  - GET /api/part/ authenticates with ``Authorization: Token <key>`` -> 200
    (and the same request without the token is rejected, proving the token
    is what authenticates)
  - OPTIONS /api/part/ exposes the InvenTree field metadata later tasks rely
    on: ``name`` (string), ``IPN``, ``active`` (boolean), ``category``
    (related field carrying ``model`` metadata)

Prints the parsed field count (>= 20 expected) plus a short excerpt.
"""

import os
import sys
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_URL = os.environ.get('INVENTREE_URL', 'http://localhost:1337')


def read_token() -> str:
    env_path = REPO_ROOT / '.env'
    for line in env_path.read_text().splitlines():
        if line.startswith('INVENTREE_TOKEN='):
            token = line.split('=', 1)[1].strip()
            if token:
                return token
    print(f'ERROR: no INVENTREE_TOKEN found in {env_path} -- run ./scripts/setup_inventree.sh first', file=sys.stderr)
    sys.exit(1)


def fail(message: str) -> None:
    print(f'FAIL: {message}', file=sys.stderr)
    sys.exit(1)


def main() -> int:
    token = read_token()
    headers = {'Authorization': f'Token {token}'}

    # --- token auth proof -------------------------------------------------------
    r = requests.get(f'{BASE_URL}/api/part/', headers=headers, timeout=10)
    if r.status_code != 200:
        fail(f'GET /api/part/ with token returned {r.status_code}: {r.text[:300]}')
    body = r.json()
    n_parts = len(body) if isinstance(body, list) else body.get('count')
    print(f'GET /api/part/ with token -> {r.status_code} (parts={n_parts})')

    anonymous = requests.get(f'{BASE_URL}/api/part/', timeout=10)
    if anonymous.status_code not in (401, 403):
        fail(f'GET /api/part/ without token returned {anonymous.status_code} (expected 401/403)')
    print(f'GET /api/part/ without token -> {anonymous.status_code} (token auth proven)')

    # --- OPTIONS metadata --------------------------------------------------------
    r = requests.options(f'{BASE_URL}/api/part/', headers=headers, timeout=10)
    if r.status_code != 200:
        fail(f'OPTIONS /api/part/ returned {r.status_code}: {r.text[:300]}')
    metadata = r.json()

    fields = metadata.get('actions', {}).get('POST', {})
    if not fields:
        fail(f'no POST action fields in OPTIONS metadata: {str(metadata)[:300]}')

    def field(name: str) -> dict:
        if name not in fields:
            fail(f'field {name!r} missing from OPTIONS /api/part/ metadata')
        return fields[name]

    checks = {
        'name is string': field('name').get('type') == 'string',
        'IPN present': 'IPN' in fields,
        'active is boolean': field('active').get('type') == 'boolean',
        'category is related field': field('category').get('type') == 'related field',
        'category has model metadata': isinstance(field('category').get('model'), str)
        and bool(fields['category']['model']),
    }
    for description, ok in checks.items():
        print(f'  {"ok" if ok else "FAIL"}: {description}')
    if not all(checks.values()):
        fail(f'OPTIONS metadata checks failed: {checks}')

    count = len(fields)
    print(f'OPTIONS /api/part/ parsed field count: {count} (expected >= 20)')
    if count < 20:
        fail(f'only {count} fields parsed from OPTIONS /api/part/ metadata')

    excerpt = {name: fields[name] for name in ('name', 'IPN', 'active', 'category') if name in fields}
    print(f'metadata excerpt: {excerpt}')

    print('smoke test passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
