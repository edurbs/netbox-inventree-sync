"""Seed the local InvenTree dev instance: admin user and API token.

This script is executed INSIDE the inventree-web container by
scripts/setup_inventree.sh (the 1.7.0 image ships no ``inventree-shell``
entry point, so the Django management shell of the packaged backend is the
documented equivalent):

    docker compose exec -T -w /home/inventree/src/backend/InvenTree \\
        inventree-web python3 manage.py shell < scripts/seed_inventree.py

It creates/updates the ``admin`` superuser (password ``admin``) and ensures a
legacy v1 ``users.models.ApiToken`` named ``netbox-sync`` exists for it.  A
superuser's token is unrestricted (all rulesets/scopes), and the v1 variant
stores the key in plaintext, so re-runs can re-print the very same key and
the token authenticates with plain ``Authorization: Token <key>`` (not JWT).

The key is printed as a final ``INVENTREE_TOKEN=<key>`` line, which
scripts/setup_inventree.sh captures into the repo root ``.env``.

NOTE: this file is piped into the Django shell, which exec()s it -- so there
is deliberately no ``if __name__ == '__main__'`` guard or argument parsing.

Idempotent: re-running resets the password and re-uses the existing token.
"""

import django

django.setup()  # harmless no-op when already set up by manage.py

from django.contrib.auth import get_user_model  # noqa: E402

from users.models import ApiToken  # noqa: E402

ADMIN_USERNAME = 'admin'
ADMIN_PASSWORD = 'admin'
ADMIN_EMAIL = 'admin@example.com'
TOKEN_NAME = 'netbox-sync'

User = get_user_model()

user = User.objects.filter(username=ADMIN_USERNAME).first()
created = user is None
if user is None:
    user = User(username=ADMIN_USERNAME, email=ADMIN_EMAIL)
user.is_active = True
user.is_staff = True
user.is_superuser = True
user.set_password(ADMIN_PASSWORD)
user.save()
print(f"admin user: {'created' if created else 'updated'} ({ADMIN_USERNAME}/{ADMIN_PASSWORD})")

token = ApiToken.objects.filter(user=user, name=TOKEN_NAME, token_version=1).first()
if token is None:
    token = ApiToken.objects.create(
        user=user,
        name=TOKEN_NAME,
        # v1-style plaintext key: re-readable on re-runs, works with
        # "Authorization: Token <key>" (hmac-based v2 secrets are shown once)
        key=ApiToken.generate_key(),
        token_version=1,
    )
    print('API token: created (v1, full access via superuser)')
else:
    if token.revoked:
        token.revoked = False
        token.revocation_reason = None
        token.save(update_fields=['revoked', 'revocation_reason'])
    print('API token: reused existing (v1, full access via superuser)')

# Machine-readable final line, consumed by scripts/setup_inventree.sh.
print(f'INVENTREE_TOKEN={token.key}')
