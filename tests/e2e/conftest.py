"""Shared fixtures for the end-to-end suite against the live NetBox + InvenTree pair.

The suite talks to REAL services only:

* NetBox v4.7 dev server (``scripts/start.sh`` runserver) — REST API for
  fixtures and the plugin's own REST API for connections/mappings/synced
  objects. Deterministic sync runs go through ``manage.py inventree_sync``
  (the E2E determinism tool) as a subprocess against the same database the
  server uses.
* InvenTree 1.7 (docker compose ``inventree-web``) — REST API.

Credentials and URLs come from the environment with defaults read from the
repo root ``.env`` (parsed by a tiny helper; no python-dotenv dependency):
``NETBOX_URL``, ``NETBOX_TOKEN``, ``INVENTREE_URL``, ``INVENTREE_TOKEN``.

Every test cleans up what it created in BOTH systems. The ``cleanup``
fixture owns the whole ordered teardown: drain in-flight plugin jobs,
disable the run's mappings (stops signal enqueues), delete NetBox
fixtures, delete InvenTree fixtures, delete plugin link rows via a Django
shell subprocess (mapping deletion is PROTECT'd by them), delete the
mappings, delete the connection. Everything is best-effort — teardown
warns instead of failing so one stranded object never hides a real
regression (the next run's unique ``e2e-<run>`` prefixes keep leftovers
harmless).
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

# The suite is run from netbox/ (`./venv/bin/python -m pytest ../tests/e2e`);
# repo root (docker-compose.yml, netbox venv, .env) is two levels above us.
REPO_ROOT = Path(__file__).resolve().parents[2]
VENV_PY = REPO_ROOT / 'netbox' / 'venv' / 'bin' / 'python'
MANAGE = ['netbox/netbox/manage.py']

# Job statuses that mean "the worker has not finished with this job yet".
# Recurring 'scheduled' rows are excluded on purpose: they sit in the
# future by design and would never drain.
_PENDING_JOB_STATUSES = ('pending', 'running')

# Reconcile interval for E2E mappings: one week, so the recurring schedule
# never fires mid-test (the deterministic command is the test driver).
RECONCILE_INTERVAL_MINUTES = 10_080


def _load_env_file(path: Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines of a .env file (no quoting magic)."""
    env = {}
    if not path.is_file():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        env[key.strip()] = value.strip()
    return env


_ENV_FILE = _load_env_file(REPO_ROOT / '.env')


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name) or _ENV_FILE.get(name) or default
    if not value:
        pytest.fail(f'Required setting {name!r} not set (env or {REPO_ROOT / ".env"})')
    return value


def poll_until(predicate, *, timeout=60.0, interval=1.0, description='condition'):
    """Poll ``predicate()`` every ``interval``s until truthy or ``timeout``.

    Returns the last truthy value on success; raises AssertionError with
    the elapsed time and ``description`` otherwise.
    """
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f'Timed out after {timeout:.0f}s waiting for {description}')


def _results(session, url, **params):
    """GET ``url`` and normalize DRF pagination to a plain list."""
    payload = session.get(url, params=params or None, timeout=15).json()
    return payload if isinstance(payload, list) else payload.get('results', [])


# --- API sessions ---------------------------------------------------------------


@pytest.fixture(scope='session')
def netbox_url() -> str:
    return _env('NETBOX_URL', 'http://localhost:8000').rstrip('/')


@pytest.fixture(scope='session')
def inventree_url() -> str:
    return _env('INVENTREE_URL', 'http://localhost:1337').rstrip('/')


@pytest.fixture(scope='session')
def netbox_api(netbox_url) -> requests.Session:
    """Authenticated NetBox REST session; fails fast when the server is down."""
    session = requests.Session()
    session.headers['Authorization'] = f"Token {_env('NETBOX_TOKEN')}"
    session.headers['Accept'] = 'application/json'
    response = session.get(f'{netbox_url}/api/', timeout=15)
    if response.status_code != 200:
        pytest.fail(
            f'NetBox at {netbox_url} is not reachable (HTTP {response.status_code}); '
            'start the stack with ./scripts/start.sh'
        )
    return session


@pytest.fixture(scope='session')
def inventree_api(inventree_url) -> requests.Session:
    """Authenticated InvenTree REST session; fails fast when the server is down."""
    session = requests.Session()
    session.headers['Authorization'] = f"Token {_env('INVENTREE_TOKEN')}"
    session.headers['Accept'] = 'application/json'
    response = session.get(f'{inventree_url}/api/', timeout=15)
    if response.status_code != 200:
        pytest.fail(
            f'InvenTree at {inventree_url} is not reachable (HTTP {response.status_code}); '
            'start the stack with docker compose up -d'
        )
    return session


@pytest.fixture(scope='session')
def run_id() -> str:
    """Unique prefix for every object this run creates (e2e-<uuid8>)."""
    return f'e2e-{uuid.uuid4().hex[:8]}'


# --- plugin REST API --------------------------------------------------------------


@pytest.fixture(scope='session')
def plugin_base(netbox_url) -> str:
    """Absolute base URL of the plugin's REST API."""
    return f'{netbox_url}/api/plugins/netbox_inventree_sync'


@pytest.fixture
def plugin_api(netbox_api, plugin_base):
    """Read helpers over the plugin's REST API (uses the netbox_api session)."""

    def _mapping_rows(mapping_id):
        """All synced-object link rows of one mapping."""
        response = netbox_api.get(f'{plugin_base}/synced-objects/', params={
            'mapping_id': mapping_id, 'limit': 0,
        }, timeout=15)
        assert response.status_code == 200, response.text
        return response.json()['results']

    def _link_row(mapping_id, netbox_object_id):
        """The link row of one NetBox object under one mapping, or None."""
        for row in _mapping_rows(mapping_id):
            if row['netbox_object_id'] == netbox_object_id:
                return row
        return None

    return SimpleNamespace(
        base=plugin_base,
        mapping_rows=_mapping_rows,
        link_row=_link_row,
    )


def drain_plugin_jobs(netbox_api, netbox_url, *, timeout=90.0):
    """Block until no interval-less plugin job is pending/running.

    Recurring rows (interval set) sit scheduled in the future by design
    and are ignored. The jobs query deliberately carries NO server-side
    filters: 'status' would need one value per request (django-filter
    keeps only the last of repeated params), so pending and running are
    matched client-side over the most recent jobs. Returns True when
    drained, False on timeout.
    """
    def drained():
        response = netbox_api.get(
            f'{netbox_url}/api/core/jobs/', params={'limit': 100}, timeout=15,
        )
        if response.status_code != 200:
            return False  # transient; keep polling
        pending = [job for job in response.json().get('results', [])
                   if str(job.get('name', '')).startswith('InvenTree Sync')
                   and job.get('status') in _PENDING_JOB_STATUSES
                   and job.get('interval') is None]
        return not pending
    try:
        poll_until(drained, timeout=timeout, interval=1,
                   description='in-flight InvenTree sync jobs to finish')
        return True
    except AssertionError:
        return False


# --- ordered, best-effort cleanup -----------------------------------------------


@pytest.fixture
def cleanup(netbox_api, netbox_url, plugin_base, inventree_api, inventree_url):
    """Single owner of teardown for one test.

    Registration happens through the yielded namespace; the finalizer
    tears down in dependency order (NetBox fixtures, InvenTree fixtures,
    an InvenTree sweep for ENGINE-CREATED objects — the sync itself
    creates parts/companies/categories the tests never registered —
    plugin link rows, mappings, connection) after draining in-flight
    plugin jobs and disabling the run's mappings so no further signal
    jobs enqueue. Every step is best-effort: it warns instead of failing.
    """
    state = {
        'netbox': [],        # DELETE urls (reversed on teardown)
        'inventree': [],     # ('part'|'company'|'category', pk)
        'mappings': [],      # mapping dicts as returned by the plugin API
        'connection': None,  # connection id
        'markers': set(),    # name markers for the InvenTree sweep
    }

    def _warn(message):
        warnings.warn(f'E2E cleanup: {message}', stacklevel=2)

    def _drain_plugin_jobs():
        if not drain_plugin_jobs(netbox_api, netbox_url, timeout=90):
            _warn('plugin jobs still pending after 90s; tearing down anyway')

    def _disable_mappings():
        for mapping in state['mappings']:
            if not mapping.get('enabled'):
                continue
            response = netbox_api.patch(
                f"{plugin_base}/mappings/{mapping['id']}/",
                json={'enabled': False}, timeout=15,
            )
            if response.status_code >= 300:
                _warn(f"could not disable mapping {mapping.get('name')}: "
                      f'HTTP {response.status_code}')

    def _delete_netbox_fixtures():
        for url in reversed(state['netbox']):
            response = netbox_api.delete(url, timeout=15)
            # 404: the object is already gone (e.g. the test deleted it).
            if response.status_code >= 300 and response.status_code != 404:
                _warn(f'could not delete NetBox object {url}: HTTP {response.status_code}')

    def _delete_inventree_fixtures():
        for kind, pk in reversed(state['inventree']):
            try:
                if kind == 'part':
                    # InvenTree refuses to delete an active part.
                    inventree_api.patch(
                        f'{inventree_url}/api/part/{pk}/',
                        json={'active': False}, timeout=15,
                    )
                    response = inventree_api.delete(
                        f'{inventree_url}/api/part/{pk}/', timeout=15)
                elif kind == 'category':
                    response = inventree_api.delete(
                        f'{inventree_url}/api/part/category/{pk}/',
                        json={'delete_child_categories': True, 'delete_parts': True},
                        timeout=15,
                    )
                else:
                    response = inventree_api.delete(
                        f'{inventree_url}/api/company/{pk}/', timeout=15)
                if response.status_code >= 300:
                    _warn(f'could not delete InvenTree {kind} {pk}: '
                          f'HTTP {response.status_code}')
            except requests.RequestException as exc:
                _warn(f'error deleting InvenTree {kind} {pk}: {exc}')

    def _delete_plugin_rows():
        mapping_ids = [mapping['id'] for mapping in state['mappings']]
        if not mapping_ids:
            return
        code = (
            'from netbox_inventree_sync.models import SyncedObject, SyncFieldPair;'
            f'print(SyncedObject.objects.filter(mapping_id__in={mapping_ids}).delete(),'
            f' SyncFieldPair.objects.filter(mapping_id__in={mapping_ids}).delete())'
        )
        proc = subprocess.run(
            [str(VENV_PY), *MANAGE, 'shell', '-c', code],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=180,
        )
        if proc.returncode != 0:
            _warn(f'could not delete plugin link rows: {proc.stderr.strip()[-300:]}')

    def _delete_mappings():
        for mapping in state['mappings']:
            response = netbox_api.delete(
                f"{plugin_base}/mappings/{mapping['id']}/", timeout=15)
            if response.status_code >= 300:
                _warn(f"could not delete mapping {mapping.get('name')}: "
                      f'HTTP {response.status_code}')

    def _delete_connection():
        if state['connection'] is None:
            return
        response = netbox_api.delete(
            f"{plugin_base}/connections/{state['connection']}/", timeout=15)
        if response.status_code >= 300:
            _warn(f"could not delete connection #{state['connection']}: "
                  f'HTTP {response.status_code}')

    def _sweep_inventree():
        """Delete ENGINE-CREATED InvenTree objects of this test.

        The sync itself publishes parts/companies/categories the test never
        registered; every name this run produces carries one of the run's
        unique markers, so anything still matching a marker after the
        explicit deletes is leftover engine output (or pre-created test
        objects whose delete failed) and goes away here.
        """
        def owned(row, *fields):
            return any(marker in str(row.get(field) or '')
                       for marker in state['markers'] for field in fields)

        try:
            for part in _results(inventree_api, f'{inventree_url}/api/part/'):
                if owned(part, 'name', 'IPN'):
                    inventree_api.patch(
                        f"{inventree_url}/api/part/{part['pk']}/",
                        json={'active': False}, timeout=15)
                    response = inventree_api.delete(
                        f"{inventree_url}/api/part/{part['pk']}/", timeout=15)
                    if response.status_code >= 300:
                        _warn(f"sweep could not delete part {part['pk']}: "
                              f'HTTP {response.status_code}')
            for company in _results(inventree_api, f'{inventree_url}/api/company/'):
                if owned(company, 'name'):
                    response = inventree_api.delete(
                        f"{inventree_url}/api/company/{company['pk']}/", timeout=15)
                    if response.status_code >= 300:
                        _warn(f"sweep could not delete company {company['pk']}: "
                              f'HTTP {response.status_code}')
            for category in _results(inventree_api, f'{inventree_url}/api/part/category/'):
                if owned(category, 'name'):
                    response = inventree_api.delete(
                        f"{inventree_url}/api/part/category/{category['pk']}/",
                        json={'delete_child_categories': True, 'delete_parts': True},
                        timeout=15)
                    if response.status_code >= 300:
                        _warn(f"sweep could not delete category {category['pk']}: "
                              f'HTTP {response.status_code}')
        except requests.RequestException as exc:
            _warn(f'InvenTree sweep failed: {exc}')

    yield SimpleNamespace(
        netbox=lambda url: state['netbox'].append(url),
        inventree=lambda kind, pk: state['inventree'].append((kind, pk)),
        mapping=lambda mapping: state['mappings'].append(mapping),
        connection=lambda connection_id: state.__setitem__('connection', connection_id),
        marker=lambda marker: state['markers'].add(marker),
    )

    # Jobs still queued when the test body finished race the row deletion
    # below (a worker mid-push re-creates the link row the shell just
    # deleted, and the PROTECT'd mapping delete then 500s). Drain first.
    _drain_plugin_jobs()
    # No new signal jobs after this point: receivers skip disabled mappings.
    _disable_mappings()
    _delete_netbox_fixtures()
    _delete_inventree_fixtures()
    _sweep_inventree()
    _delete_plugin_rows()
    _delete_mappings()
    _delete_connection()


# --- mapping factories --------------------------------------------------------------


def post_mapping(netbox_api, plugin_base, cleanup, *, name, connection, content_type,
                 endpoint, pairs, enabled=True, match=('name', 'name')):
    """POST one mapping (with nested field_pairs) via the plugin API.

    Serializer validation runs against the live InvenTree OPTIONS metadata;
    ``assert`` on anything but 201 so a rejected mapping fails loudly.
    """
    response = netbox_api.post(f'{plugin_base}/mappings/', json={
        'name': name,
        'connection': connection,
        'netbox_content_type': content_type,
        'inventree_endpoint': endpoint,
        'match_netbox_field': match[0],
        'match_inventree_field': match[1],
        'reconcile_interval_minutes': RECONCILE_INTERVAL_MINUTES,
        'enabled': enabled,
        'field_pairs': [
            {
                'netbox_field': netbox_field,
                'netbox_subfield': netbox_subfield,
                'inventree_field': inventree_field,
            }
            for netbox_field, netbox_subfield, inventree_field in pairs
        ],
    }, timeout=60)
    assert response.status_code == 201, f'{name}: {response.status_code} {response.text}'
    mapping = response.json()
    cleanup.mapping(mapping)
    return mapping


#: The three enabled starter-style mappings (inventory item -> part,
#: manufacturer -> company, role -> part category), mirroring
#: seed_starter_mappings with name/name as the match (adopt) key.
STARTER_MAPPING_SPECS = (
    ('parts', 'dcim.inventoryitem', '/api/part/', (
        ('name', '', 'name'),
        ('part_id', '', 'IPN'),
        ('description', '', 'description'),
        ('manufacturer', 'name', 'keywords'),
    )),
    ('mfr', 'dcim.manufacturer', '/api/company/', (
        ('name', '', 'name'),
        ('description', '', 'description'),
    )),
    ('role', 'dcim.inventoryitemrole', '/api/part/category/', (
        ('name', '', 'name'),
    )),
)


@pytest.fixture
def mapping_set(netbox_api, plugin_base, inventree_url, run_id, cleanup):
    """One dedicated connection plus the three enabled starter-style mappings."""
    cleanup.marker(run_id)
    response = netbox_api.post(f'{plugin_base}/connections/', json={
        'name': f'{run_id}-conn',
        'server_url': inventree_url,
        'api_token': _env('INVENTREE_TOKEN'),
        'enabled': True,
    }, timeout=30)
    assert response.status_code == 201, response.text
    connection = response.json()
    cleanup.connection(connection['id'])

    mappings = {
        key: post_mapping(
            netbox_api, plugin_base, cleanup,
            name=f'{run_id}-{key}',
            connection=connection['id'],
            content_type=content_type,
            endpoint=endpoint,
            pairs=pair_specs,
        )
        for key, content_type, endpoint, pair_specs in STARTER_MAPPING_SPECS
    }

    return SimpleNamespace(
        run=run_id,
        connection=connection,
        parts=mappings['parts'],
        mfr=mappings['mfr'],
        role=mappings['role'],
    )


@pytest.fixture
def mapping_factory(netbox_api, plugin_base, mapping_set, cleanup):
    """Extra mappings on the mapping_set's connection (scenario-local).

    Note the model's unique constraint: one mapping per (connection,
    content type, endpoint). A second mapping for the same triple — e.g.
    another inventoryitem -> /api/part/ variant — needs its OWN
    connection; use ``scoped_connection`` for that.
    """

    def _factory(*, name, content_type, endpoint, pairs, enabled=True, match=('name', 'name')):
        return post_mapping(
            netbox_api, plugin_base, cleanup,
            name=name,
            connection=mapping_set.connection['id'],
            content_type=content_type,
            endpoint=endpoint,
            pairs=pairs,
            enabled=enabled,
            match=match,
        )

    return _factory


@pytest.fixture
def scoped_connection(netbox_api, plugin_base, inventree_url, cleanup):
    """A dedicated connection (own unique-constraint namespace) for
    scenario-local mapping sets that would collide with mapping_set's
    (connection, content type, endpoint) triples."""
    name = f'scoped-{uuid.uuid4().hex[:8]}'
    response = netbox_api.post(f'{plugin_base}/connections/', json={
        'name': name,
        'server_url': inventree_url,
        'api_token': _env('INVENTREE_TOKEN'),
        'enabled': True,
    }, timeout=30)
    assert response.status_code == 201, response.text
    cleanup.connection(response.json()['id'])
    cleanup.marker(name)
    return response.json()


# --- deterministic sync + docker helpers ---------------------------------------------


def sync_command(mapping_name: str, *extra: str) -> subprocess.CompletedProcess:
    """Run ``manage.py inventree_sync --mapping <name> [extra...]`` inline.

    Uses the dev configuration (the live server's database); no worker
    involved. Returncode is 1 when any processed object failed.
    """
    return subprocess.run(
        [str(VENV_PY), *MANAGE, 'inventree_sync', '--mapping', mapping_name, *extra],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300,
    )


def compose(*args: str) -> subprocess.CompletedProcess:
    """Run ``docker compose <args>`` in the repo root."""
    return subprocess.run(
        ['docker', 'compose', *args],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300,
    )


def wait_inventree_healthy(inventree_api, inventree_url, *, timeout=90.0):
    """Poll ``GET /api/`` until InvenTree answers 200 again."""
    def healthy():
        try:
            return inventree_api.get(f'{inventree_url}/api/', timeout=10).status_code == 200
        except requests.RequestException:
            return False
    poll_until(healthy, timeout=timeout, interval=2, description='InvenTree to come back up')


# --- NetBox fixture factory -----------------------------------------------------------


@pytest.fixture
def nb(netbox_api, netbox_url, run_id, cleanup):
    """Factory for NetBox fixture chains; everything registers for cleanup."""
    cleanup.marker(run_id)
    base = f'{netbox_url}/api'

    def _post(path, payload):
        response = netbox_api.post(f'{base}/{path}/', json=payload, timeout=30)
        assert response.status_code == 201, f'{path}: {response.status_code} {response.text}'
        row = response.json()
        cleanup.netbox(row['url'])
        return row

    def make_chain(*, suffix='', part_id=None, description=None, serial=None,
                   item_name=None, role=None, custom_fields=None):
        """Create manufacturer/site/role/device-type/device + inventory item.

        Every object registers for cleanup; ``role`` may be an existing
        inventory-item-role dict to point the item at.
        """
        prefix = f'{run_id}{suffix}'
        mfr = _post('dcim/manufacturers', {
            'name': f'{prefix}-mfr', 'slug': f'{prefix}-mfr',
            'description': f'{prefix} manufacturer',
        })
        site = _post('dcim/sites', {'name': f'{prefix}-site', 'slug': f'{prefix}-site'})
        device_role = _post('dcim/device-roles', {
            'name': f'{prefix}-drole', 'slug': f'{prefix}-drole',
        })
        device_type = _post('dcim/device-types', {
            'manufacturer': mfr['id'], 'model': f'{prefix}-model',
            'slug': f'{prefix}-model', 'u_height': 1,
        })
        device = _post('dcim/devices', {
            'name': f'{prefix}-dev', 'device_type': device_type['id'],
            'role': device_role['id'], 'site': site['id'],
        })
        item = _post('dcim/inventory-items', {
            'device': device['id'],
            'name': item_name or f'{prefix}-item',
            'manufacturer': mfr['id'],
            'role': role['id'] if role else None,
            'part_id': part_id or f'{prefix}-PART',
            'serial': serial or 'SN-0001',
            'description': description if description is not None else f'{prefix} description',
            'custom_fields': custom_fields or {},
        })
        return SimpleNamespace(
            manufacturer=mfr, site=site, device_role=device_role,
            device_type=device_type, device=device, item=item,
        )

    def make_role(name):
        return _post('dcim/inventory-item-roles', {'name': name, 'slug': name})

    return SimpleNamespace(post=_post, make_chain=make_chain, make_role=make_role)
