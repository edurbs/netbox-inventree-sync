"""UI smoke: the mapping builder renders both field lists with compatibility data.

Logs into the NetBox UI with the form/CSRF cookie dance (the seeded admin
user), opens the builder page of the run's parts mapping and checks:

* both field lists are offered (a NetBox field and an InvenTree field),
* the page's embedded fields JSON (the same matrix ``/fields/`` serves)
  marks the boolean NetBox field ``discovered`` as NOT compatible with
  the string InvenTree field ``IPN`` — the client-side greying's source
  of truth.
"""

from __future__ import annotations

import json
import re

import requests

BUILDERS_SCRIPT = re.compile(
    r'<script id="builder-fields" type="application/json">(.*?)</script>', re.DOTALL,
)


def test_mapping_builder_renders(netbox_url, mapping_set):
    session = requests.Session()

    login_page = session.get(f'{netbox_url}/login/', timeout=15)
    assert login_page.status_code == 200, 'login page unreachable'
    response = session.post(
        f'{netbox_url}/login/',
        data={
            'username': 'admin',
            'password': 'admin',
            'csrfmiddlewaretoken': session.cookies.get('csrftoken'),
        },
        headers={'Referer': f'{netbox_url}/login/'},
        timeout=15,
        allow_redirects=False,
    )
    assert response.status_code in (301, 302), f'login failed: {response.status_code}'

    builder_url = (
        f"{netbox_url}/plugins/netbox_inventree_sync/sync-mappings/"
        f"{mapping_set.parts['id']}/builder/"
    )
    page = session.get(builder_url, timeout=30)
    assert page.status_code == 200, f'builder page: {page.status_code}'
    html = page.text

    # Both columns render: the NetBox side offers InventoryItem fields,
    # the InvenTree side the /api/part/ fields.
    assert 'NetBox field' in html
    assert 'InvenTree field' in html
    assert re.search(r'Serial Number \(serial\)', html), 'NetBox field list missing serial'
    assert re.search(r'IPN \(string\)', html), 'InvenTree field list missing IPN'

    # The embedded matrix carries the compatibility verdicts: boolean
    # 'discovered' is not accepted by string 'IPN' (text 'part_id' is).
    match = BUILDERS_SCRIPT.search(html)
    assert match, 'builder page embeds no fields JSON script block'
    fields = json.loads(match.group(1))
    inventree_specs = {spec['name']: spec for spec in fields['inventree']}
    assert 'part_id' in inventree_specs['IPN']['compatible_netbox_fields']
    assert 'discovered' not in inventree_specs['IPN']['compatible_netbox_fields']
