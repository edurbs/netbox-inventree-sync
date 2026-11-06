"""Engine tests with a recording FakeClient (hermetic; no HTTP, no mocks).

The nine pinned tests cover the per-object flow: claim, payload build, hash
fast-path, full-payload PATCH, adoption policy (0/1/N candidates, blank
key), error containment for API and transport failures, the second-job
no-re-create contract and log redaction. The extra cases below pin
``reconcile_mapping``'s stats contract, ``deactivate`` and every
``extract_value`` branch.
"""

import hashlib
import json
from datetime import date

import requests
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from dcim.models import Manufacturer, Region, Site
from extras.models import Tag

from netbox_inventree_sync.choices import (
    SyncActionChoices,
    SyncStatusChoices,
    TransformChoices,
)
from netbox_inventree_sync.models import (
    InvenTreeInstance,
    SyncFieldPair,
    SyncLogEntry,
    SyncMapping,
    SyncedObject,
)
from netbox_inventree_sync.sync.client import InvenTreeClientError
from netbox_inventree_sync.sync.engine import SyncEngine
from netbox_inventree_sync.sync.payload import extract_value

FAKE_TOKEN = 'test-token-xyz'
ENDPOINT = '/api/company/'


def canonical_hash(payload):
    """sha256 of canonical JSON (sorted keys) — mirrors the engine contract."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class FakeClient:
    """Stands in for :class:`~netbox_inventree_sync.sync.client.InvenTreeClient`.

    Records every post/patch/match call and serves canned data; each
    ``*_error`` slot turns that one verb into a raised exception.
    """

    def __init__(self):
        self.posts = []      # (endpoint, data)
        self.patches = []    # (endpoint, pk, data)
        self.matches = []    # (endpoint, field, value)
        self.match_results = []
        self.post_response = {'pk': 101, 'name': 'Acme'}
        self.patch_response = {'pk': 101, 'name': 'Acme'}
        self.post_error = None
        self.patch_error = None
        self.match_error = None

    def get_field_specs(self, endpoint):
        return []

    def post(self, endpoint, data):
        self.posts.append((endpoint, data))
        if self.post_error is not None:
            raise self.post_error
        return json.loads(json.dumps(self.post_response))

    def patch(self, endpoint, pk, data):
        self.patches.append((endpoint, pk, data))
        if self.patch_error is not None:
            raise self.patch_error
        return json.loads(json.dumps(self.patch_response))

    def match(self, endpoint, field, value):
        self.matches.append((endpoint, field, value))
        if self.match_error is not None:
            raise self.match_error
        return json.loads(json.dumps(self.match_results))


class SyncEngineTestCase(TestCase):
    """The nine pinned per-object flows, driven through ``sync_object``."""

    @classmethod
    def setUpTestData(cls):
        cls.connection = InvenTreeInstance.objects.create(
            name='Primary InvenTree',
            url='http://inventree.test',
            api_token=FAKE_TOKEN,
        )

    def setUp(self):
        self.fake = FakeClient()

    # --- fixtures -------------------------------------------------------------

    def make_mapping(self, *, match=False):
        kwargs = {
            'name': 'Manufacturers to companies',
            'connection': self.connection,
            'netbox_content_type': ContentType.objects.get_for_model(Manufacturer),
            'inventree_endpoint': ENDPOINT,
            'enabled': True,
        }
        if match:
            kwargs['match_netbox_field'] = 'name'
            kwargs['match_inventree_field'] = 'name'
        return SyncMapping.objects.create(**kwargs)

    def map_name_and_slug(self, mapping):
        self.pair(mapping, 'name', 'name')
        self.pair(mapping, 'slug', 'description')

    def pair(self, mapping, netbox_field, inventree_field, *, netbox_subfield='', transform=TransformChoices.NONE):
        return SyncFieldPair.objects.create(
            mapping=mapping,
            netbox_field=netbox_field,
            netbox_subfield=netbox_subfield,
            inventree_field=inventree_field,
            transform=transform,
        )

    def make_manufacturer(self, name='Acme'):
        return Manufacturer.objects.create(name=name, slug=name.lower() or 'unnamed')

    # --- the nine pinned tests --------------------------------------------------

    def test_create_when_no_link_and_always_create(self):
        mapping = self.make_mapping()
        self.map_name_and_slug(mapping)
        mfr = self.make_manufacturer()
        payload = {'name': 'Acme', 'description': 'acme'}

        synced = SyncEngine(self.connection, client=self.fake).sync_object(mapping, mfr.pk)

        self.assertEqual(self.fake.posts, [(ENDPOINT, payload)])
        self.assertEqual(self.fake.patches, [])
        # No match fields configured: the mapping always creates, never adopts.
        self.assertEqual(self.fake.matches, [])
        self.assertEqual(synced.status, SyncStatusChoices.SYNCED)
        self.assertEqual(synced.inventree_pk, 101)
        self.assertEqual(synced.last_payload_hash, canonical_hash(payload))
        self.assertEqual(synced.last_error, '')
        log = synced.log_entries.get()
        self.assertEqual(log.action, SyncActionChoices.CREATE)
        self.assertEqual(log.payload, payload)

    def test_update_patches_diff_only(self):
        mapping = self.make_mapping()
        self.map_name_and_slug(mapping)
        mfr = self.make_manufacturer()
        engine = SyncEngine(self.connection, client=self.fake)
        engine.sync_object(mapping, mfr.pk)  # initial create (pk 101)

        mfr.name = 'Acme Renamed'
        mfr.save()
        payload = {'name': 'Acme Renamed', 'description': 'acme'}

        synced = engine.sync_object(mapping, mfr.pk)

        # Exactly one PATCH whose body is the FULL mapped payload: slug is
        # unchanged yet still sent (controller ruling on PATCH semantics).
        self.assertEqual(len(self.fake.posts), 1)
        self.assertEqual(self.fake.patches, [(ENDPOINT, 101, payload)])
        self.assertEqual(synced.status, SyncStatusChoices.SYNCED)
        self.assertEqual(synced.last_payload_hash, canonical_hash(payload))
        self.assertEqual(synced.log_entries.first().action, SyncActionChoices.UPDATE)
        calls = (len(self.fake.posts), len(self.fake.patches), len(self.fake.matches))

        # Identical payload hash -> zero HTTP calls of any kind.
        synced_again = engine.sync_object(mapping, mfr.pk)
        self.assertEqual(
            (len(self.fake.posts), len(self.fake.patches), len(self.fake.matches)), calls,
        )
        self.assertEqual(synced_again.status, SyncStatusChoices.SYNCED)

    def test_adopt_single_exact_match(self):
        mapping = self.make_mapping(match=True)
        self.pair(mapping, 'name', 'name')
        mfr = self.make_manufacturer()
        self.fake.match_results = [{'pk': 77, 'name': 'Acme'}]
        payload = {'name': 'Acme'}

        synced = SyncEngine(self.connection, client=self.fake).sync_object(mapping, mfr.pk)

        self.assertEqual(self.fake.matches, [(ENDPOINT, 'name', 'Acme')])
        self.assertEqual(self.fake.posts, [])
        # Adoption stores the link and PATCHes the drift in one go.
        self.assertEqual(self.fake.patches, [(ENDPOINT, 77, payload)])
        self.assertEqual(synced.status, SyncStatusChoices.SYNCED)
        self.assertEqual(synced.inventree_pk, 77)
        self.assertEqual(synced.last_error, '')
        self.assertEqual(synced.log_entries.get().action, SyncActionChoices.ADOPT)

    def test_ambiguous_match_flags_never_adopts(self):
        mapping = self.make_mapping(match=True)
        self.pair(mapping, 'name', 'name')
        mfr = self.make_manufacturer()
        self.fake.match_results = [
            {'pk': 77, 'name': 'Acme'},
            {'pk': 88, 'name': 'Acme'},
        ]

        synced = SyncEngine(self.connection, client=self.fake).sync_object(mapping, mfr.pk)

        self.assertEqual(synced.status, SyncStatusChoices.FAILED)
        self.assertIn('ambiguous match', synced.last_error)
        self.assertIsNone(synced.inventree_pk)
        self.assertEqual(self.fake.posts, [])
        self.assertEqual(self.fake.patches, [])

    def test_blank_match_key_flags(self):
        mapping = self.make_mapping(match=True)
        self.pair(mapping, 'name', 'name')
        mfr = self.make_manufacturer(name='')

        synced = SyncEngine(self.connection, client=self.fake).sync_object(mapping, mfr.pk)

        self.assertEqual(synced.status, SyncStatusChoices.FAILED)
        self.assertIn('blank match key', synced.last_error)
        self.assertIsNone(synced.inventree_pk)
        # A blank key never reaches the remote: no lookup, no post.
        self.assertEqual(self.fake.matches, [])
        self.assertEqual(self.fake.posts, [])

    def test_client_error_marks_failed_and_logs(self):
        mapping = self.make_mapping()
        self.pair(mapping, 'name', 'name')
        mfr = self.make_manufacturer()
        self.fake.post_error = InvenTreeClientError(
            'InvenTree API POST /api/company/ returned HTTP 400',
            status=400,
            body='{"name": ["This field must be unique."]}',
        )

        synced = SyncEngine(self.connection, client=self.fake).sync_object(mapping, mfr.pk)

        self.assertEqual(synced.status, SyncStatusChoices.FAILED)
        self.assertIn('HTTP 400', synced.last_error)
        log = synced.log_entries.get()
        self.assertEqual(log.action, SyncActionChoices.ERROR)
        self.assertEqual(log.response_status, 400)
        self.assertIn('must be unique', log.response_body)

    def test_connection_error_marks_failed(self):
        mapping = self.make_mapping()
        self.pair(mapping, 'name', 'name')
        mfr = self.make_manufacturer()
        self.fake.post_error = requests.ConnectionError('Connection refused')

        synced = SyncEngine(self.connection, client=self.fake).sync_object(mapping, mfr.pk)

        self.assertEqual(synced.status, SyncStatusChoices.FAILED)
        self.assertIn('Connection refused', synced.last_error)
        log = synced.log_entries.get()
        self.assertEqual(log.action, SyncActionChoices.ERROR)
        self.assertIsNone(log.response_status)

    def test_concurrent_claims_create_once(self):
        mapping = self.make_mapping()
        self.pair(mapping, 'name', 'name')
        mfr = self.make_manufacturer()
        # Another job already claimed this object and finished its POST: the
        # link row exists (status still pending) with the remote pk stored.
        SyncedObject.objects.create(
            mapping=mapping,
            netbox_object_id=mfr.pk,
            status=SyncStatusChoices.PENDING,
            inventree_pk=7,
        )

        synced = SyncEngine(self.connection, client=self.fake).sync_object(mapping, mfr.pk)

        # The second job takes the update/no-op path: no re-create remotely.
        self.assertLessEqual(len(self.fake.posts), 1)
        self.assertEqual(self.fake.posts, [])
        self.assertEqual(len(self.fake.patches), 1)
        endpoint, pk, _ = self.fake.patches[0]
        self.assertEqual((endpoint, pk), (ENDPOINT, 7))
        self.assertEqual(synced.status, SyncStatusChoices.SYNCED)
        self.assertEqual(synced.log_entries.get().action, SyncActionChoices.UPDATE)

    def test_token_redacted_in_logs(self):
        mapping = self.make_mapping()
        self.pair(mapping, 'name', 'name')
        mfr = self.make_manufacturer()
        leaked = (
            f'auth failed for token {FAKE_TOKEN} '
            f'via {self.connection.url}?token={FAKE_TOKEN}'
        )
        self.fake.post_error = InvenTreeClientError('denied', status=403, body=leaked)

        synced = SyncEngine(self.connection, client=self.fake).sync_object(mapping, mfr.pk)

        self.assertEqual(synced.status, SyncStatusChoices.FAILED)
        for entry in SyncLogEntry.objects.all():
            blob = json.dumps(entry.payload) + entry.response_body
            self.assertNotIn(FAKE_TOKEN, blob)
            self.assertNotIn(self.connection.url, blob)
        self.assertNotIn(FAKE_TOKEN, synced.last_error)
        # Redaction strips only the secrets, not the diagnostic content.
        self.assertIn('auth failed', synced.log_entries.get().response_body)


class ReconcileMappingTestCase(TestCase):
    """``reconcile_mapping`` stats and its linked/unlinked split."""

    @classmethod
    def setUpTestData(cls):
        cls.connection = InvenTreeInstance.objects.create(
            name='Primary InvenTree',
            url='http://inventree.test',
            api_token=FAKE_TOKEN,
        )

    def setUp(self):
        self.fake = FakeClient()

    def test_reconcile_refreshes_links_and_adopts_new_objects(self):
        mapping = SyncMapping.objects.create(
            name='Manufacturers to companies',
            connection=self.connection,
            netbox_content_type=ContentType.objects.get_for_model(Manufacturer),
            inventree_endpoint=ENDPOINT,
            enabled=True,
            match_netbox_field='name',
            match_inventree_field='name',
        )
        SyncFieldPair.objects.create(mapping=mapping, netbox_field='name', inventree_field='name')
        linked = Manufacturer.objects.create(name='Acme', slug='acme')
        fresh = Manufacturer.objects.create(name='Beta', slug='beta')
        SyncedObject.objects.create(
            mapping=mapping,
            netbox_object_id=linked.pk,
            inventree_pk=5,
            status=SyncStatusChoices.SYNCED,
            last_payload_hash=canonical_hash({'name': 'Acme'}),
        )
        self.fake.match_results = [{'pk': 9, 'name': 'Beta'}]

        stats = SyncEngine(self.connection, client=self.fake).reconcile_mapping(mapping)

        self.assertEqual(stats, {
            'created': 0,
            'updated': 0,
            'adopted': 1,
            'failed': 0,
            'orphaned': 0,
            'unchanged': 1,  # hash fast-path: the linked object costs no HTTP
        })
        self.assertEqual(self.fake.matches, [(ENDPOINT, 'name', 'Beta')])
        self.assertEqual(self.fake.posts, [])
        self.assertEqual(self.fake.patches, [(ENDPOINT, 9, {'name': 'Beta'})])


class DeactivateTestCase(TestCase):
    """``deactivate`` is local-only state transition (remote work comes later)."""

    def test_deactivate_marks_orphaned_and_logs(self):
        connection = InvenTreeInstance.objects.create(
            name='Primary InvenTree', url='http://inventree.test', api_token=FAKE_TOKEN,
        )
        mapping = SyncMapping.objects.create(
            name='Manufacturers to companies',
            connection=connection,
            netbox_content_type=ContentType.objects.get_for_model(Manufacturer),
            inventree_endpoint=ENDPOINT,
        )
        mfr = Manufacturer.objects.create(name='Acme', slug='acme')
        synced = SyncedObject.objects.create(
            mapping=mapping,
            netbox_object_id=mfr.pk,
            inventree_pk=5,
            status=SyncStatusChoices.SYNCED,
        )

        SyncEngine(connection, client=FakeClient()).deactivate(synced)

        synced.refresh_from_db()
        self.assertEqual(synced.status, SyncStatusChoices.ORPHANED)
        # The remote pk is kept so later cleanup can still find the object.
        self.assertEqual(synced.inventree_pk, 5)
        self.assertEqual(synced.log_entries.get().action, SyncActionChoices.DEACTIVATE)


class ExtractValueTestCase(TestCase):
    """Every ``extract_value`` dispatch branch and transform."""

    @classmethod
    def setUpTestData(cls):
        cls.region = Region.objects.create(name='EMEA', slug='emea')
        cls.site = Site.objects.create(name='HQ', slug='hq', region=cls.region)
        cls.site.tags.add(
            Tag.objects.create(name='Alpha', slug='alpha'),
            Tag.objects.create(name='Beta', slug='beta'),
        )
        # Custom-field values are read from the instance dict; the types NetBox
        # keeps there (str, int, date) exercise each transform directly.
        cls.site.custom_field_data = {
            'ext_id': 'X-1',
            'capacity': 42,
            'go_live': date(2026, 1, 31),
        }

    def pair(self, netbox_field, *, netbox_subfield='', transform=TransformChoices.NONE):
        return SyncFieldPair(
            netbox_field=netbox_field,
            netbox_subfield=netbox_subfield,
            inventree_field='target',
            transform=transform,
        )

    def test_plain_field(self):
        self.assertEqual(extract_value(self.site, self.pair('name')), 'HQ')

    def test_missing_field_returns_none(self):
        self.assertIsNone(extract_value(self.site, self.pair('nonexistent')))

    def test_custom_field_reads_custom_field_data(self):
        self.assertEqual(extract_value(self.site, self.pair('ext_id')), 'X-1')

    def test_tags_join_slugs(self):
        self.assertEqual(extract_value(self.site, self.pair('tags')), 'alpha,beta')

    def test_pointer_as_text_uses_subfield(self):
        pair = self.pair('region', netbox_subfield='name')
        self.assertEqual(extract_value(self.site, pair), 'EMEA')

    def test_pointer_as_text_without_target_returns_none(self):
        site = Site.objects.create(name='Branch', slug='branch')
        pair = self.pair('region', netbox_subfield='name')
        self.assertIsNone(extract_value(site, pair))

    def test_iso_date_transform(self):
        pair = self.pair('go_live', transform=TransformChoices.ISO_DATE)
        self.assertEqual(extract_value(self.site, pair), '2026-01-31')

    def test_str_transform(self):
        pair = self.pair('capacity', transform=TransformChoices.STR)
        self.assertEqual(extract_value(self.site, pair), '42')
