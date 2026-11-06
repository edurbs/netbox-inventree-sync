"""Pointer resolution, delete semantics and authoritative reconciliation.

The pinned tests extend the Task 8 engine contract: pointer→related pairs
resolve depth-first through enabled pairing mappings (the referenced
manufacturer is published as a company BEFORE the part references it),
missing pairings fail the referencer without any payload, remote drift on
synced links is verified against the live InvenTree object and repaired,
and a deleted NetBox object orphans its link while the remote object is
deactivated — never deleted. Extra cases pin the recursion depth guard and
the pointer-as-match-key guard carried from the Task 8 review.
"""

import json

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from dcim.models import Device, DeviceRole, DeviceType, InventoryItem, Manufacturer, Site
from dcim.models import Region

from netbox_inventree_sync.choices import SyncActionChoices, SyncStatusChoices
from netbox_inventree_sync.models import (
    InvenTreeInstance,
    SyncFieldPair,
    SyncLogEntry,
    SyncMapping,
    SyncedObject,
)
from netbox_inventree_sync.sync.client import InvenTreeClientError, InventreeFieldSpec
from netbox_inventree_sync.sync.engine import (
    MAX_POINTER_DEPTH,
    PairingResolutionError,
    SyncEngine,
)

FAKE_TOKEN = 'test-token-xyz'
COMPANY_ENDPOINT = '/api/company/'
PART_ENDPOINT = '/api/part/'


def spec(name, type_, *, required=False, max_length=None):
    """One writable InvenTree field spec for the fake's OPTIONS data."""
    return InventreeFieldSpec(
        name=name, type=type_, required=required,
        max_length=max_length, related_model=None,
    )


# /api/company/ in InvenTree 1.7.0 carries a writable boolean 'active'.
COMPANY_SPECS = [
    spec('name', 'string', required=True, max_length=100),
    spec('description', 'string', max_length=250),
    spec('active', 'boolean'),
    spec('is_customer', 'boolean'),
]
# /api/part/category/ has NO 'active' — its only writable boolean is
# 'structural'; used to pin the log-only delete path.
CATEGORY_STYLE_SPECS = [
    spec('name', 'string', required=True, max_length=100),
    spec('structural', 'boolean'),
]


class FakeClient:
    """Stands in for :class:`~netbox_inventree_sync.sync.client.InvenTreeClient`.

    Records every get/post/patch/match call (``calls`` keeps the cross-verb
    order), serves ``remote[pk]`` from GET (unknown pk → HTTP 404 error) and
    auto-assigns increasing pks on POST; ``field_specs`` feeds
    ``get_field_specs`` per endpoint.
    """

    def __init__(self):
        self.calls = []       # (verb, endpoint, ...) in call order
        self.posts = []       # (endpoint, data)
        self.patches = []     # (endpoint, pk, data)
        self.gets = []        # (endpoint, pk)
        self.matches = []     # (endpoint, field, value)
        self.match_results = []
        self.next_pk = 100    # last pk handed out by post()
        self.remote = {}      # pk -> dict served by get()
        self.field_specs = {}  # endpoint -> list[InventreeFieldSpec]
        self.patch_error = None

    def get_field_specs(self, endpoint):
        return list(self.field_specs.get(endpoint, []))

    def get(self, endpoint, pk):
        self.calls.append(('get', endpoint, pk))
        self.gets.append((endpoint, pk))
        if pk not in self.remote:
            raise InvenTreeClientError(
                f'InvenTree API GET {endpoint}{pk}/ returned HTTP 404',
                status=404,
                body='Not Found',
            )
        return json.loads(json.dumps(self.remote[pk]))

    def post(self, endpoint, data):
        self.calls.append(('post', endpoint, data))
        self.posts.append((endpoint, data))
        self.next_pk += 1
        return {'pk': self.next_pk, **json.loads(json.dumps(data))}

    def patch(self, endpoint, pk, data):
        self.calls.append(('patch', endpoint, pk, data))
        self.patches.append((endpoint, pk, data))
        if self.patch_error is not None:
            raise self.patch_error
        return {'pk': pk, **json.loads(json.dumps(data))}

    def match(self, endpoint, field, value):
        self.matches.append((endpoint, field, value))
        return json.loads(json.dumps(self.match_results))


class PointerResolutionTestCase(TestCase):
    """Depth-first pointer→related resolution through pairing mappings."""

    @classmethod
    def setUpTestData(cls):
        cls.connection = InvenTreeInstance.objects.create(
            name='Primary InvenTree',
            url='http://inventree.test',
            api_token=FAKE_TOKEN,
        )
        cls.manufacturer = Manufacturer.objects.create(name='Acme', slug='acme')
        site = Site.objects.create(name='HQ', slug='hq')
        role = DeviceRole.objects.create(name='Server', slug='server')
        device_type = DeviceType.objects.create(
            manufacturer=cls.manufacturer, model='X-1', slug='x-1',
        )
        device = Device.objects.create(
            name='dev1', device_type=device_type, role=role, site=site,
        )
        cls.item = InventoryItem.objects.create(
            device=device, manufacturer=cls.manufacturer, name='PSU',
        )

    def setUp(self):
        self.fake = FakeClient()

    def make_mappings(self, *, pairing_enabled=True):
        part = SyncMapping.objects.create(
            name='Inventory items to parts',
            connection=self.connection,
            netbox_content_type=ContentType.objects.get_for_model(InventoryItem),
            inventree_endpoint=PART_ENDPOINT,
            enabled=True,
        )
        SyncFieldPair.objects.create(
            mapping=part, netbox_field='name', inventree_field='name',
        )
        SyncFieldPair.objects.create(
            mapping=part, netbox_field='manufacturer', inventree_field='category',
        )
        company = SyncMapping.objects.create(
            name='Manufacturers to companies',
            connection=self.connection,
            netbox_content_type=ContentType.objects.get_for_model(Manufacturer),
            inventree_endpoint=COMPANY_ENDPOINT,
            enabled=pairing_enabled,
        )
        SyncFieldPair.objects.create(
            mapping=company, netbox_field='name', inventree_field='name',
        )
        return part, company

    def test_company_created_before_part_references_it(self):
        part, company = self.make_mappings()

        synced = SyncEngine(self.connection, client=self.fake).sync_object(part, self.item.pk)

        # Depth-first: the pairing POST precedes the part POST, and the
        # part carries the freshly created company pk as its FK value.
        self.assertEqual(self.fake.posts, [
            (COMPANY_ENDPOINT, {'name': 'Acme'}),
            (PART_ENDPOINT, {'name': 'PSU', 'category': 101}),
        ])
        self.assertEqual(synced.status, SyncStatusChoices.SYNCED)
        self.assertEqual(synced.inventree_pk, 102)
        company_link = SyncedObject.objects.get(
            mapping=company, netbox_object_id=self.manufacturer.pk,
        )
        self.assertEqual(company_link.status, SyncStatusChoices.SYNCED)
        self.assertEqual(company_link.inventree_pk, 101)

    def test_pairing_disabled_fails_object_without_post(self):
        part, _company = self.make_mappings(pairing_enabled=False)

        synced = SyncEngine(self.connection, client=self.fake).sync_object(part, self.item.pk)

        self.assertEqual(synced.status, SyncStatusChoices.FAILED)
        self.assertIn('pairing', synced.last_error.lower())
        self.assertIsNone(synced.inventree_pk)
        # No payload was published anywhere: not the part, not the company.
        self.assertEqual(self.fake.posts, [])
        self.assertEqual(self.fake.patches, [])

    def test_pointer_depth_guard_raises(self):
        part, _company = self.make_mappings()
        engine = SyncEngine(self.connection, client=self.fake)
        engine._pointer_depth = MAX_POINTER_DEPTH
        pair = part.field_pairs.get(netbox_field='manufacturer')

        with self.assertRaises(PairingResolutionError):
            engine.resolve_pointer(pair, self.item)
        # The guard fires before any HTTP traffic.
        self.assertEqual(self.fake.calls, [])


class PointerMatchKeyTestCase(TestCase):
    """A pointer used as match key is treated as a blank key, never an object."""

    def test_pointer_match_field_treated_as_blank_key(self):
        connection = InvenTreeInstance.objects.create(
            name='Primary InvenTree', url='http://inventree.test', api_token=FAKE_TOKEN,
        )
        region = Region.objects.create(name='EMEA', slug='emea')
        site = Site.objects.create(name='HQ', slug='hq', region=region)
        mapping = SyncMapping.objects.create(
            name='Sites to companies',
            connection=connection,
            netbox_content_type=ContentType.objects.get_for_model(Site),
            inventree_endpoint=COMPANY_ENDPOINT,
            match_netbox_field='region',
            match_inventree_field='name',
        )
        SyncFieldPair.objects.create(
            mapping=mapping, netbox_field='name', inventree_field='name',
        )
        fake = FakeClient()

        synced = SyncEngine(connection, client=fake).sync_object(mapping, site.pk)

        self.assertEqual(synced.status, SyncStatusChoices.FAILED)
        self.assertIn('blank match key', synced.last_error)
        self.assertIn('pointer', synced.last_error)
        # The related object itself is never used as a lookup key.
        self.assertEqual(fake.matches, [])
        self.assertEqual(fake.posts, [])
        self.assertEqual(fake.patches, [])


class DeleteSemanticsTestCase(TestCase):
    """``handle_delete``: orphan locally, deactivate remotely, never delete."""

    @classmethod
    def setUpTestData(cls):
        cls.connection = InvenTreeInstance.objects.create(
            name='Primary InvenTree',
            url='http://inventree.test',
            api_token=FAKE_TOKEN,
        )
        cls.mapping = SyncMapping.objects.create(
            name='Manufacturers to companies',
            connection=cls.connection,
            netbox_content_type=ContentType.objects.get_for_model(Manufacturer),
            inventree_endpoint=COMPANY_ENDPOINT,
            enabled=True,
        )
        SyncFieldPair.objects.create(
            mapping=cls.mapping, netbox_field='name', inventree_field='name',
        )

    def setUp(self):
        self.fake = FakeClient()

    def make_link(self, *, status=SyncStatusChoices.SYNCED, inventree_pk=5):
        mfr = Manufacturer.objects.create(name='Acme', slug='acme')
        synced = SyncedObject.objects.create(
            mapping=self.mapping,
            netbox_object_id=mfr.pk,
            status=status,
            inventree_pk=inventree_pk,
        )
        return mfr, synced

    def test_deactivate_on_delete(self):
        mfr, synced = self.make_link()
        self.fake.field_specs[COMPANY_ENDPOINT] = COMPANY_SPECS
        netbox_object_id = mfr.pk  # pk reads as None once deleted
        mfr.delete()

        result = SyncEngine(self.connection, client=self.fake).handle_delete(
            self.mapping, netbox_object_id,
        )

        self.assertEqual(result.status, SyncStatusChoices.ORPHANED)
        # The remote object is deactivated, never deleted: its pk is kept.
        self.assertEqual(result.inventree_pk, 5)
        self.assertEqual(self.fake.patches, [(COMPANY_ENDPOINT, 5, {'active': False})])
        log = result.log_entries.get()
        self.assertEqual(log.action, SyncActionChoices.DEACTIVATE)
        self.assertEqual(log.payload, {'active': False})

    def test_delete_without_active_field_logs_only(self):
        mfr, synced = self.make_link()
        self.fake.field_specs[COMPANY_ENDPOINT] = CATEGORY_STYLE_SPECS
        netbox_object_id = mfr.pk  # pk reads as None once deleted
        mfr.delete()

        result = SyncEngine(self.connection, client=self.fake).handle_delete(
            self.mapping, netbox_object_id,
        )

        self.assertEqual(result.status, SyncStatusChoices.ORPHANED)
        # No writable boolean 'active' on the endpoint: local transition
        # only, with the reason noted on the deactivate log entry.
        self.assertEqual(self.fake.patches, [])
        log = result.log_entries.get()
        self.assertEqual(log.action, SyncActionChoices.DEACTIVATE)
        self.assertIn('active', log.response_body)


class ReconcileDriftTestCase(TestCase):
    """``reconcile_mapping`` verifies synced links against the remote truth."""

    @classmethod
    def setUpTestData(cls):
        cls.connection = InvenTreeInstance.objects.create(
            name='Primary InvenTree',
            url='http://inventree.test',
            api_token=FAKE_TOKEN,
        )
        cls.mapping = SyncMapping.objects.create(
            name='Manufacturers to companies',
            connection=cls.connection,
            netbox_content_type=ContentType.objects.get_for_model(Manufacturer),
            inventree_endpoint=COMPANY_ENDPOINT,
            enabled=True,
        )
        SyncFieldPair.objects.create(
            mapping=cls.mapping, netbox_field='name', inventree_field='name',
        )
        SyncFieldPair.objects.create(
            mapping=cls.mapping, netbox_field='description', inventree_field='description',
        )
        cls.mfr = Manufacturer.objects.create(name='Acme', slug='acme')

    def setUp(self):
        self.fake = FakeClient()

    def test_reconcile_repairs_remote_drift(self):
        engine = SyncEngine(self.connection, client=self.fake)
        engine.sync_object(self.mapping, self.mfr.pk)  # initial create -> pk 101
        self.assertEqual(self.fake.posts, [
            (COMPANY_ENDPOINT, {'name': 'Acme', 'description': ''}),
        ])
        synced = SyncedObject.objects.get(
            mapping=self.mapping, netbox_object_id=self.mfr.pk,
        )

        # The payload hash still matches, yet the remote description was
        # edited out-of-band: only the authoritative GET can see this.
        self.fake.remote[101] = {
            'pk': 101, 'name': 'Acme', 'description': 'tampered remotely',
        }
        stats = engine.reconcile_mapping(self.mapping)

        self.assertEqual(stats['updated'], 1)
        self.assertEqual(self.fake.patches, [
            (COMPANY_ENDPOINT, 101, {'name': 'Acme', 'description': ''}),
        ])
        synced.refresh_from_db()
        self.assertEqual(synced.status, SyncStatusChoices.SYNCED)
        self.assertEqual(synced.log_entries.first().action, SyncActionChoices.UPDATE)

        # Identical remote: verified again but left alone — no second PATCH.
        self.fake.remote[101] = {'pk': 101, 'name': 'Acme', 'description': ''}
        stats = engine.reconcile_mapping(self.mapping)
        self.assertEqual(stats['unchanged'], 1)
        self.assertEqual(len(self.fake.patches), 1)


class ReconcileMissingObjectsTestCase(TestCase):
    """Reconcile flags gone NetBox objects; orphaned links are never clobbered."""

    @classmethod
    def setUpTestData(cls):
        cls.connection = InvenTreeInstance.objects.create(
            name='Primary InvenTree',
            url='http://inventree.test',
            api_token=FAKE_TOKEN,
        )
        cls.mapping = SyncMapping.objects.create(
            name='Manufacturers to companies',
            connection=cls.connection,
            netbox_content_type=ContentType.objects.get_for_model(Manufacturer),
            inventree_endpoint=COMPANY_ENDPOINT,
            enabled=True,
        )
        SyncFieldPair.objects.create(
            mapping=cls.mapping, netbox_field='name', inventree_field='name',
        )

    def setUp(self):
        self.fake = FakeClient()
        self.fake.field_specs[COMPANY_ENDPOINT] = COMPANY_SPECS

    def test_reconcile_flags_missing_netbox_objects(self):
        gone = Manufacturer.objects.create(name='Gone', slug='gone')
        missing = Manufacturer.objects.create(name='Missing', slug='missing')
        fresh = Manufacturer.objects.create(name='Fresh', slug='fresh')
        gone_link = SyncedObject.objects.create(
            mapping=self.mapping,
            netbox_object_id=gone.pk,
            inventree_pk=5,
            status=SyncStatusChoices.SYNCED,
            last_payload_hash='stale',
        )
        orphan_link = SyncedObject.objects.create(
            mapping=self.mapping,
            netbox_object_id=missing.pk,
            inventree_pk=6,
            status=SyncStatusChoices.ORPHANED,
        )
        gone.delete()
        missing.delete()
        orphan_logs_before = SyncLogEntry.objects.filter(synced=orphan_link).count()

        stats = SyncEngine(self.connection, client=self.fake).reconcile_mapping(self.mapping)

        gone_link.refresh_from_db()
        orphan_link.refresh_from_db()
        # The synced link whose object vanished is orphaned and the remote
        # object deactivated.
        self.assertEqual(gone_link.status, SyncStatusChoices.ORPHANED)
        self.assertEqual(self.fake.patches, [(COMPANY_ENDPOINT, 5, {'active': False})])
        # The already-orphaned link stays orphaned: no re-fail, no second
        # deactivate PATCH, no new log entries.
        self.assertEqual(orphan_link.status, SyncStatusChoices.ORPHANED)
        self.assertEqual(
            SyncLogEntry.objects.filter(synced=orphan_link).count(), orphan_logs_before,
        )
        # The unlinked survivor is published.
        self.assertEqual(self.fake.posts, [(COMPANY_ENDPOINT, {'name': 'Fresh'})])
        self.assertEqual(stats, {
            'created': 1,
            'updated': 0,
            'adopted': 0,
            'failed': 0,
            'orphaned': 1,
            'unchanged': 0,
        })
