"""The spec's ten end-to-end sync scenarios over the live NetBox + InvenTree pair.

Scenarios 1-9 drive the sync deterministically through
``manage.py inventree_sync`` (no worker wait); scenario 10 proves the
production async path (API write -> signal -> RQ worker -> InvenTree)
with the worker started by ``scripts/start.sh``.

Note on the live RQ worker: it runs while these tests execute and may
process the very same signal-triggered jobs concurrently with the
deterministic command. That is fine by design — the engine's row locks
serialize pushes and every assertion here is an end-state assertion.
"""

from __future__ import annotations

from .conftest import (
    compose,
    drain_plugin_jobs,
    poll_until,
    post_mapping,
    sync_command,
    wait_inventree_healthy,
)


# --- helpers ----------------------------------------------------------------------


def find_part(inventree_api, inventree_url, **params):
    """First InvenTree part matching exact-field ``params``.

    Note ``?IPN=`` is a registered exact filter on ``/api/part/`` while
    ``?name=`` is silently ignored by InvenTree 1.7 (see
    ``InvenTreeClient.match``); prefer ``IPN`` lookups here — a ``name``
    lookup would scan the whole first page, which is enough for the tiny
    E2E catalogs but is NOT an exact server-side filter.
    """
    parts = inventree_api.get(f'{inventree_url}/api/part/', params=params, timeout=15)
    assert parts.status_code == 200, parts.text
    payload = parts.json()
    results = payload if isinstance(payload, list) else payload.get('results', [])
    return results[0] if results else None


def get_part(inventree_api, inventree_url, pk):
    response = inventree_api.get(f'{inventree_url}/api/part/{pk}/', timeout=15)
    assert response.status_code == 200, response.text
    return response.json()


def sync_object(mapping, model_ref):
    """Deterministically sync one NetBox object under ``mapping``."""
    proc = sync_command(mapping['name'], '--object', model_ref)
    assert proc.returncode == 0, (
        f'inventree_sync failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}'
    )
    return proc


def reconcile(mapping):
    """Run one full deterministic reconcile pass for ``mapping``."""
    proc = sync_command(mapping['name'], '--reconcile')
    assert proc.returncode == 0, (
        f'inventree_sync --reconcile failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}'
    )
    return proc


# --- 1. create ----------------------------------------------------------------------


def test_create_inventory_item_creates_part(mapping_set, nb, inventree_api,
                                            inventree_url, plugin_api):
    """Scenario 1: a new inventory item becomes a part with all mapped fields."""
    chain = nb.make_chain(suffix='-c1')

    sync_object(mapping_set.parts, f"dcim.inventoryitem:{chain.item['id']}")

    part = find_part(inventree_api, inventree_url, IPN=chain.item['part_id'])
    assert part is not None, f"no InvenTree part with IPN {chain.item['part_id']}"
    assert part['name'] == chain.item['name']
    assert part['description'] == chain.item['description']
    assert chain.manufacturer['name'] in (part['keywords'] or '')
    assert part['active'] is True

    link = plugin_api.link_row(mapping_set.parts['id'], chain.item['id'])
    assert link is not None, 'no synced-object link row for the item'
    assert link['status'] == 'synced'
    assert link['inventree_pk'] == part['pk']


# --- 2. update ------------------------------------------------------------------------


def test_update_propagates(mapping_set, nb, netbox_api, inventree_api,
                           inventree_url, plugin_api):
    """Scenario 2: changing a mapped NetBox field propagates on the next sync."""
    chain = nb.make_chain(suffix='-c2')
    sync_object(mapping_set.parts, f"dcim.inventoryitem:{chain.item['id']}")
    link = plugin_api.link_row(mapping_set.parts['id'], chain.item['id'])
    part = get_part(inventree_api, inventree_url, link['inventree_pk'])
    assert part['description'] == chain.item['description']

    new_description = f'{mapping_set.run} updated description'
    response = netbox_api.patch(
        chain.item['url'], json={'description': new_description}, timeout=15,
    )
    assert response.status_code == 200, response.text
    sync_object(mapping_set.parts, f"dcim.inventoryitem:{chain.item['id']}")

    part = get_part(inventree_api, inventree_url, link['inventree_pk'])
    assert part['description'] == new_description
    assert part['IPN'] == chain.item['part_id']  # untouched pair stayed put


# --- 3. delete --------------------------------------------------------------------------


def test_delete_deactivates_never_deletes(mapping_set, nb, netbox_api,
                                          inventree_api, inventree_url, plugin_api):
    """Scenario 3: deleting the NetBox item deactivates the part, never deletes it."""
    chain = nb.make_chain(suffix='-c3')
    sync_object(mapping_set.parts, f"dcim.inventoryitem:{chain.item['id']}")
    link = plugin_api.link_row(mapping_set.parts['id'], chain.item['id'])
    part_pk = link['inventree_pk']

    response = netbox_api.delete(chain.item['url'], timeout=15)
    assert response.status_code in (200, 204), response.text
    # The delete pass runs through the deterministic reconcile (pass 3).
    reconcile(mapping_set.parts)

    link = plugin_api.link_row(mapping_set.parts['id'], chain.item['id'])
    assert link['status'] == 'orphaned'
    assert link['inventree_pk'] == part_pk  # kept locatable
    part = get_part(inventree_api, inventree_url, part_pk)
    assert part['active'] is False  # deactivated, NOT deleted
    assert part['IPN'] == chain.item['part_id']


# --- 4. pointer pairing order --------------------------------------------------------------


def test_pointer_pairing_order(netbox_api, netbox_url, plugin_base, scoped_connection, nb,
                               inventree_api, inventree_url, cleanup, plugin_api):
    """Scenario 4: dependencies publish first; the part references nothing partial.

    One flow: manufacturer and role exist only in NetBox when the part
    sync starts. The manufacturer is published under its pairing mapping
    first (company), and the part mapping's pointer->related pair
    (``role`` -> ``category``) resolves depth-first — the category exists
    in InvenTree before the part is posted, and the part carries its pk.
    The sync-log's create timestamps prove the publication order.

    The scenario carries its own connection so its inventoryitem ->
    /api/part/ mapping (with the pointer pair) does not collide with the
    starter set's triple on the shared connection.
    """
    mfr_mapping = post_mapping(
        netbox_api, plugin_base, cleanup,
        name=f'{scoped_connection["name"]}-mfr', connection=scoped_connection['id'],
        content_type='dcim.manufacturer', endpoint='/api/company/',
        pairs=(('name', '', 'name'), ('description', '', 'description')),
    )
    role_mapping = post_mapping(
        netbox_api, plugin_base, cleanup,
        name=f'{scoped_connection["name"]}-role', connection=scoped_connection['id'],
        content_type='dcim.inventoryitemrole', endpoint='/api/part/category/',
        pairs=(('name', '', 'name'),),
    )
    ptr_mapping = post_mapping(
        netbox_api, plugin_base, cleanup,
        name=f'{scoped_connection["name"]}-ptr', connection=scoped_connection['id'],
        content_type='dcim.inventoryitem', endpoint='/api/part/',
        pairs=(
            ('name', '', 'name'),
            ('part_id', '', 'IPN'),
            ('role', '', 'category'),  # pointer -> related through the role pairing
        ),
    )

    chain = nb.make_chain(suffix='-c4', role=nb.make_role(f'{scoped_connection["name"]}-role'))

    # Let the worker settle the signal jobs of the chain creation, so the
    # ordering assertions below observe a deterministic sequence: the
    # pairing publishes (company, then category) strictly before the part.
    assert drain_plugin_jobs(netbox_api, netbox_url), 'worker did not settle in time'

    # Publish the manufacturer under its pairing mapping first (company).
    sync_object(mfr_mapping, f"dcim.manufacturer:{chain.manufacturer['id']}")
    # Part sync resolves the role pairing depth-first, then posts the part.
    sync_object(ptr_mapping, f"dcim.inventoryitem:{chain.item['id']}")

    companies = inventree_api.get(
        f'{inventree_url}/api/company/', timeout=15).json()
    companies = companies if isinstance(companies, list) else companies.get('results', [])
    company = next(c for c in companies if c['name'] == chain.manufacturer['name'])
    categories = inventree_api.get(
        f'{inventree_url}/api/part/category/', timeout=15).json()
    categories = categories if isinstance(categories, list) else categories.get('results', [])
    category = next(c for c in categories if c['name'] == chain.item['role']['name'])

    part = find_part(inventree_api, inventree_url, IPN=chain.item['part_id'])
    assert part is not None
    # The part carries the resolved pairing pk — never a partial reference.
    assert part['category'] == category['pk']

    # Dependency order, proven by the plugin's own audit log: the sync-log
    # create entries (µs-precision timestamps) of the pairing objects
    # precede the part's. (Cross-table InvenTree pks prove nothing — each
    # table has its own sequence.)
    log_rows = netbox_api.get(
        f'{plugin_api.base}/sync-log/', params={'limit': 100}, timeout=15,
    ).json()['results']
    created_at = {}
    for target, name in (('company', chain.manufacturer['name']),
                         ('category', chain.item['role']['name']),
                         ('part', chain.item['name'])):
        entries = [row for row in log_rows
                   if row['action'] == 'create'
                   and (row.get('payload') or {}).get('name') == name]
        assert entries, f'no create log entry for the {target} ({name!r})'
        created_at[target] = entries[0]['created']
    assert created_at['company'] < created_at['part'], \
        'the company was not published before the part referencing its manufacturer'
    assert created_at['category'] < created_at['part'], \
        'the category was not published before the part referencing it'

    # The depth-first resolution also linked the role under its pairing.
    role_link = plugin_api.link_row(role_mapping['id'], chain.item['role']['id'])
    assert role_link is not None and role_link['status'] == 'synced'
    assert role_link['inventree_pk'] == category['pk']


# --- 5. incompatible pair rejected ---------------------------------------------------------


def test_incompatible_pair_rejected_by_api(mapping_set, netbox_api, plugin_base,
                                           inventree_api, inventree_url):
    """Scenario 5: the API rejects a boolean->string pair with HTTP 400."""
    def inventree_part_pks():
        payload = inventree_api.get(f'{inventree_url}/api/part/', timeout=15).json()
        results = payload if isinstance(payload, list) else payload.get('results', [])
        return {part['pk'] for part in results}

    pks_before = inventree_part_pks()
    response = netbox_api.post(f'{plugin_base}/mappings/', json={
        'name': f'{mapping_set.run}-bad',
        'connection': mapping_set.connection['id'],
        'netbox_content_type': 'dcim.inventoryitem',
        'inventree_endpoint': '/api/part/',
        'match_netbox_field': 'name',
        'match_inventree_field': 'name',
        'enabled': True,
        'field_pairs': [
            # 'discovered' is a boolean on InventoryItem; 'description' is a
            # string on /api/part/ — outside the compatibility matrix.
            {'netbox_field': 'discovered', 'netbox_subfield': '',
             'inventree_field': 'description'},
        ],
    }, timeout=60)
    assert response.status_code == 400, response.text
    assert 'not compatible' in response.text

    # The rejected mapping never existed, so nothing can ever sync through it.
    mappings = netbox_api.get(f'{plugin_base}/mappings/', params={'limit': 0},
                              timeout=15).json()['results']
    assert not [m for m in mappings if m['name'] == f'{mapping_set.run}-bad']
    assert inventree_part_pks() == pks_before, 'objects appeared despite rejection'


# --- 6. adopt existing by match key -----------------------------------------------------------


def test_adopt_existing_by_match_key(mapping_set, nb, inventree_api,
                                      inventree_url, cleanup, plugin_api):
    """Scenario 6: a pre-existing InvenTree part with the match key is adopted."""
    existing_ipn = f'X-EXISTING-{mapping_set.run}'
    item_name = f'{mapping_set.run}-c6-item'
    pre = inventree_api.post(f'{inventree_url}/api/part/', json={
        'name': item_name,           # == the NetBox item's name (match key)
        'IPN': existing_ipn,
        'description': 'pre-existing manual part',
        'active': True,
    }, timeout=15)
    assert pre.status_code == 201, pre.text
    cleanup.inventree('part', pre.json()['pk'])

    chain = nb.make_chain(suffix='-c6', item_name=item_name, part_id=existing_ipn,
                          description='adopted by e2e')

    sync_object(mapping_set.parts, f"dcim.inventoryitem:{chain.item['id']}")

    # The pre-existing part was ADOPTED: same pk, drift (description) patched,
    # and no duplicate was created.
    link = plugin_api.link_row(mapping_set.parts['id'], chain.item['id'])
    assert link is not None and link['status'] == 'synced'
    assert link['inventree_pk'] == pre.json()['pk']

    payload = inventree_api.get(f'{inventree_url}/api/part/',
                                params={'IPN': existing_ipn}, timeout=15).json()
    matches = payload if isinstance(payload, list) else payload.get('results', [])
    assert len(matches) == 1, f'expected exactly one part with IPN {existing_ipn}'
    assert matches[0]['description'] == 'adopted by e2e'


# --- 7. outage marks failed, reconcile heals ------------------------------------------------------


def test_outage_marks_failed_and_reconcile_heals(mapping_set, nb, inventree_api,
                                                 inventree_url, plugin_api):
    """Scenario 7: an InvenTree outage fails the link; a reconcile heals it."""
    chain = nb.make_chain(suffix='-c7')

    stopped = compose('stop', 'inventree-web')
    assert stopped.returncode == 0, stopped.stderr
    try:
        proc = sync_command(mapping_set.parts['name'],
                            '--object', f"dcim.inventoryitem:{chain.item['id']}")
        assert proc.returncode == 1, (
            f'sync against a stopped InvenTree should exit 1:\n{proc.stdout}\n{proc.stderr}'
        )
        link = plugin_api.link_row(mapping_set.parts['id'], chain.item['id'])
        assert link is not None, 'failed push left no link row'
        assert link['status'] == 'failed'
        assert link['last_error'], 'failure reason not recorded on the link row'
        assert link['inventree_pk'] is None, 'nothing was linked while InvenTree was down'
    finally:
        started = compose('start', 'inventree-web')
        assert started.returncode == 0, started.stderr
        wait_inventree_healthy(inventree_api, inventree_url)

    reconcile(mapping_set.parts)

    link = plugin_api.link_row(mapping_set.parts['id'], chain.item['id'])
    assert link['status'] == 'synced'
    part = get_part(inventree_api, inventree_url, link['inventree_pk'])
    assert part['IPN'] == chain.item['part_id']


# --- 8. manual InvenTree edit overwritten by reconcile -------------------------------------------


def test_manual_inventree_edit_overwritten_by_reconcile(mapping_set, nb, inventree_api,
                                                        inventree_url, plugin_api):
    """Scenario 8: drift introduced directly in InvenTree is repaired by reconcile."""
    chain = nb.make_chain(suffix='-c8')
    sync_object(mapping_set.parts, f"dcim.inventoryitem:{chain.item['id']}")
    link = plugin_api.link_row(mapping_set.parts['id'], chain.item['id'])

    drifted = f'{mapping_set.run} manual drift'
    response = inventree_api.patch(
        f"{inventree_url}/api/part/{link['inventree_pk']}/",
        json={'description': drifted}, timeout=15,
    )
    assert response.status_code == 200, response.text
    assert get_part(inventree_api, inventree_url,
                    link['inventree_pk'])['description'] == drifted

    reconcile(mapping_set.parts)

    link = plugin_api.link_row(mapping_set.parts['id'], chain.item['id'])
    part = get_part(inventree_api, inventree_url, link['inventree_pk'])
    assert part['description'] == chain.item['description']
    assert link['last_error'] == ''


# --- 9. custom field mapping ------------------------------------------------------------------------


def test_custom_field_mapping(netbox_api, netbox_url, plugin_base, scoped_connection,
                              nb, inventree_api, inventree_url, plugin_api, cleanup):
    """Scenario 9: a NetBox custom field maps through to InvenTree."""
    field_name = 'supplier_code'
    created = netbox_api.post(f'{netbox_url}/api/extras/custom-fields/', json={
        'name': field_name,
        'label': 'Supplier Code',
        'type': 'text',
        'object_types': ['dcim.inventoryitem'],
        'active': True,
    }, timeout=30)
    if created.status_code == 201:
        cleanup.netbox(created.json()['url'])
    else:
        # A previous run's field lingers; reuse it.
        assert created.status_code == 400, created.text

    # Own connection: this inventoryitem -> /api/part/ mapping (with the
    # custom-field pair) is the only one targeting the item.
    cf_mapping = post_mapping(
        netbox_api, plugin_base, cleanup,
        name=f'{scoped_connection["name"]}-cf', connection=scoped_connection['id'],
        content_type='dcim.inventoryitem', endpoint='/api/part/',
        pairs=(
            ('name', '', 'name'),
            ('part_id', '', 'IPN'),
            (field_name, '', 'keywords'),  # custom text field -> string
        ),
    )

    supplier_code = f'SUP-{scoped_connection["name"]}-42'
    chain = nb.make_chain(suffix='-c9', custom_fields={field_name: supplier_code})

    sync_object(cf_mapping, f"dcim.inventoryitem:{chain.item['id']}")

    part = find_part(inventree_api, inventree_url, IPN=chain.item['part_id'])
    assert part is not None
    assert part['keywords'] == supplier_code
    link = plugin_api.link_row(cf_mapping['id'], chain.item['id'])
    assert link is not None and link['status'] == 'synced'


# --- 10. signal -> job async path ---------------------------------------------------------------------


def test_signal_to_job_async_path(mapping_set, nb, netbox_api, netbox_url,
                                  inventree_api, inventree_url, plugin_api):
    """Scenario 10: an API write syncs through the RQ worker, no manual command."""
    workers = netbox_api.get(f'{netbox_url}/api/core/background-workers/',
                             timeout=15).json()
    assert workers['count'] >= 1, (
        'no RQ worker running; start the stack with ./scripts/start.sh '
        '(the async path needs it)'
    )

    chain = nb.make_chain(suffix='-c10')

    # No inventree_sync call: the post_save signal enqueues the ObjectSyncJob
    # and the worker drives the engine. Poll for the link row to settle.
    link = poll_until(
        lambda: (lambda row: row if row and row['status'] == 'synced' else None)(
            plugin_api.link_row(mapping_set.parts['id'], chain.item['id'])
        ),
        timeout=60, interval=1,
        description=f"worker to sync inventory item #{chain.item['id']}",
    )

    part = get_part(inventree_api, inventree_url, link['inventree_pk'])
    assert part['IPN'] == chain.item['part_id']
    assert part['description'] == chain.item['description']

    # The machinery really ran as a background job (not the test process).
    jobs = netbox_api.get(
        f'{netbox_url}/api/core/jobs/',
        params=[('status', 'completed'), ('limit', '100')],
        timeout=15,
    ).json()
    jobs = jobs if isinstance(jobs, list) else jobs.get('results', [])
    completed = [
        job for job in jobs
        if job.get('name') == 'InvenTree Sync: Object'
        and job.get('object_id') == mapping_set.parts['id']
        and str((job.get('data') or {}).get('netbox_object', '')).endswith(
            f":{chain.item['id']}")
    ]
    assert completed, 'no completed InvenTree Sync: Object job row for the item'
