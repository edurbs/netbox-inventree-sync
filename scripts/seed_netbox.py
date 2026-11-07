#!/usr/bin/env python3
"""Seed the local NetBox dev instance: admin user and API token.

Creates/updates the ``admin`` superuser (password ``admin``), ensures an API
token exists for it, and writes that token to ``NETBOX_TOKEN`` in the repo
root ``.env`` for later tasks to use.

Run from the repository root with the NetBox venv:

    netbox/venv/bin/python scripts/seed_netbox.py

Idempotent: re-running resets the password and reuses (or creates) the token.
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Django project root inside the NetBox clone (contains manage.py and the
# 'netbox' package), so 'netbox.settings' resolves when added to sys.path.
PROJECT_ROOT = REPO_ROOT / 'netbox' / 'netbox'

ADMIN_USERNAME = 'admin'
ADMIN_PASSWORD = 'admin'


def main() -> int:
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'netbox.settings')
    sys.path.insert(0, str(PROJECT_ROOT))

    import django

    django.setup()

    from users.choices import TokenVersionChoices
    from users.models import Token, User

    user, created = User.objects.get_or_create(
        username=ADMIN_USERNAME,
        defaults={'email': 'admin@example.com'},
    )
    user.is_active = True
    user.is_staff = True
    user.is_superuser = True
    user.set_password(ADMIN_PASSWORD)
    user.save()
    print(f"admin user: {'created' if created else 'updated'} ({ADMIN_USERNAME}/{ADMIN_PASSWORD})")

    # A classic (v1) token authenticates with the "Authorization: Token ..."
    # scheme used throughout this project. NetBox 4.7 defaults to v2 tokens
    # (Bearer scheme, peppered digest, plaintext shown only at creation), so
    # create v1 explicitly and keep reusing it on re-runs.
    token = Token.objects.filter(user=user, version=TokenVersionChoices.V1).first()
    if token is None:
        token = Token.objects.create(
            user=user,
            version=TokenVersionChoices.V1,
            write_enabled=True,
        )
        print('API token: created (v1)')
    else:
        print('API token: reused existing (v1)')

    env_path = REPO_ROOT / '.env'
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    token_line = f'NETBOX_TOKEN={token.plaintext}'
    if any(line.startswith('NETBOX_TOKEN=') for line in lines):
        lines = [token_line if line.startswith('NETBOX_TOKEN=') else line for line in lines]
    else:
        lines.append(token_line)
    env_path.write_text('\n'.join(lines) + '\n')
    print(f'NETBOX_TOKEN written to {env_path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
