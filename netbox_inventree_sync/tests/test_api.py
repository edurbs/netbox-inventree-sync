"""REST API tests: model viewsets, nested mapping writes, fields endpoint.

The InvenTree side is stubbed at the module attribute each consumer
imports — ``api.views.InvenTreeClient`` for the fields endpoint,
``sync.validation.InvenTreeClient`` for mapping writes (which route
through ``validate_mapping``) — the same pattern
``netbox_inventree_sync.tests.test_validation`` uses. No HTTP ever
leaves the process; a superuser keeps permissions trivial.
"""

from unittest import mock
from urllib.parse import urlencode

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from core.models import Job
from dcim.models import InventoryItem, InventoryItemRole, Manufacturer
from users.models import User

from netbox_inventree_sync.api import views as api_views
from netbox_inventree_sync.jobs import ReconcileJob
from netbox_inventree_sync.models import InvenTreeInstance, SyncMapping
from netbox_inventree_sync.sync import validation
from netbox_inventree_sync.sync.client import InvenTreeClientError, InventreeFieldSpec


def _string(name):
    return InventreeFieldSpec(name=name, type='string', required=False, max_length=None, related_model=None)


def _boolean(name):
    return InventreeFieldSpec(name=name, type='boolean', required=False, max_length=None, related_model=None)


def _related(name, model):
    return InventreeFieldSpec(name=name, type='related', required=False, max_length=None, related_model=model)


# Canned OPTIONS metadata per endpoint, shaped like InvenTreeClient.get_field_specs output.
ENDPOINT_SPECS = {
    '/api/part/': [
        _string('IPN'),
        _string('keywords'),
        _string('description'),
        _boolean('active'),
        _related('category', 'partcategory'),
    ],
}


class StubInvenTreeClient:
    """Stands in for :class:`~netbox_inventree_sync.sync.client.InvenTreeClient`."""

    def __init__(self, url, token, timeout=(10, 30)):
        pass

    def get_field_specs(self, endpoint):
        return list(ENDPOINT_SPECS.get(endpoint, []))


class ExplodingInvenTreeClient:
    """Client whose OPTIONS fetch fails, to pin the upstream-error response."""

    def __init__(self, url, token, timeout=(10, 30)):
        pass

    def get_field_specs(self, endpoint):
        raise InvenTreeClientError(f'InvenTree API OPTIONS {endpoint} request failed: connection refused')


class PluginAPITestCase(TestCase):
    """Superuser-authenticated API client plus a connection and pairing mappings."""

    client_class = APIClient

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='apisuper', is_superuser=True)
        cls.connection = InvenTreeInstance.objects.create(
            name='Primary InvenTree',
            url='http://inventree.test',
            api_token='secret-token',
            enabled=True,
        )
        cls.inventoryitem_ct = ContentType.objects.get_for_model(InventoryItem)
        # An enabled pairing (manufacturer) and a disabled one (role) — the
        # fields endpoint must list only the enabled content type.
        cls.manufacturer_mapping = SyncMapping.objects.create(
            name='Manufacturers to companies',
            connection=cls.connection,
            netbox_content_type=ContentType.objects.get_for_model(Manufacturer),
            inventree_endpoint='/api/company/',
            enabled=True,
        )
        cls.role_mapping = SyncMapping.objects.create(
            name='Roles to categories',
            connection=cls.connection,
            netbox_content_type=ContentType.objects.get_for_model(InventoryItemRole),
            inventree_endpoint='/api/part/category/',
            enabled=False,
        )

    def setUp(self):
        self.client.force_login(self.user)

    # --- URL helpers -------------------------------------------------------

    @staticmethod
    def url(name, **kwargs):
        return reverse(f'plugins-api:netbox_inventree_sync-api:{name}', kwargs=kwargs)

    def fields_url(self, **params):
        return f"{self.url('fields')}?{urlencode(params)}"

    def mapping_payload(self, **overrides):
        payload = {
            'name': 'Inventory items to parts',
            'connection': self.connection.pk,
            'netbox_content_type': 'dcim.inventoryitem',
            'inventree_endpoint': '/api/part/',
            'enabled': False,
            'field_pairs': [
                {'netbox_field': 'part_id', 'inventree_field': 'IPN'},
            ],
        }
        payload.update(overrides)
        return payload


class FieldsEndpointTestCase(PluginAPITestCase):

    def test_fields_endpoint_matrix(self):
        with mock.patch.object(api_views, 'InvenTreeClient', StubInvenTreeClient):
            response = self.client.get(self.fields_url(
                connection=self.connection.pk,
                content_type=self.inventoryitem_ct.pk,
                endpoint='/api/part/',
            ))

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()

        # NetBox side: full spec dicts, pointers carrying their target model.
        netbox_specs = {spec['name']: spec for spec in payload['netbox']}
        self.assertEqual(netbox_specs['part_id']['type'], 'text')
        self.assertFalse(netbox_specs['part_id']['custom'])
        self.assertEqual(netbox_specs['manufacturer']['type'], 'pointer')
        self.assertEqual(netbox_specs['manufacturer']['pointer_model'], 'dcim.manufacturer')
        self.assertIn('name', netbox_specs['manufacturer']['subfield_options'])

        # InvenTree side: full spec dicts plus the per-field compatibility list.
        inventree_specs = {spec['name']: spec for spec in payload['inventree']}
        self.assertIn('compatible_netbox_fields', inventree_specs['IPN'])

        # IPN (string) accepts the text fields — and the manufacturer pointer
        # via its subfield projection — but no boolean field.
        ipn = inventree_specs['IPN']['compatible_netbox_fields']
        self.assertIn('name', ipn)
        self.assertIn('serial', ipn)
        self.assertIn('part_id', ipn)
        self.assertIn('manufacturer', ipn)
        self.assertNotIn('discovered', ipn)

        # 'active' (boolean) has no compatible NetBox field: the spec matrix
        # has no boolean row at all (text→boolean and boolean→string are ✗).
        self.assertEqual(inventree_specs['active']['compatible_netbox_fields'], [])

        # category (related) accepts the manufacturer pointer (its pairing
        # mapping is enabled) but not the device/role pointers.
        category = inventree_specs['category']['compatible_netbox_fields']
        self.assertIn('manufacturer', category)
        self.assertNotIn('device', category)
        self.assertNotIn('role', category)

        # Only ENABLED mappings of this connection offer themselves as pairings.
        self.assertIn('dcim.manufacturer', payload['available_pairings'])
        self.assertNotIn('dcim.inventoryitemrole', payload['available_pairings'])

        # Parameter validation.
        for bad_params in (
            {},  # everything missing
            {'content_type': self.inventoryitem_ct.pk, 'endpoint': '/api/part/'},  # no connection
            {'connection': self.connection.pk, 'endpoint': '/api/part/'},  # no content type
            {'connection': self.connection.pk, 'content_type': self.inventoryitem_ct.pk},  # no endpoint
            {'connection': 99999, 'content_type': self.inventoryitem_ct.pk, 'endpoint': '/api/part/'},
            {'connection': self.connection.pk, 'content_type': 99999, 'endpoint': '/api/part/'},
        ):
            response = self.client.get(self.fields_url(**bad_params))
            self.assertEqual(response.status_code, 400, f'{bad_params}: {response.content}')

        # A disabled connection is rejected too.
        self.connection.enabled = False
        self.connection.save()
        try:
            response = self.client.get(self.fields_url(
                connection=self.connection.pk,
                content_type=self.inventoryitem_ct.pk,
                endpoint='/api/part/',
            ))
            self.assertEqual(response.status_code, 400, response.content)
        finally:
            self.connection.enabled = True
            self.connection.save()

        # An upstream OPTIONS failure surfaces as a 502-style error response.
        with mock.patch.object(api_views, 'InvenTreeClient', ExplodingInvenTreeClient):
            response = self.client.get(self.fields_url(
                connection=self.connection.pk,
                content_type=self.inventoryitem_ct.pk,
                endpoint='/api/part/',
            ))
        self.assertEqual(response.status_code, 502, response.content)
        self.assertIn('connection refused', str(response.data))


class MappingWriteTestCase(PluginAPITestCase):

    def test_mapping_write_rejects_incompatible_pair(self):
        # discovered (boolean) -> description (string) violates the matrix.
        payload = self.mapping_payload(field_pairs=[
            {'netbox_field': 'discovered', 'inventree_field': 'description'},
        ])
        with mock.patch.object(validation, 'InvenTreeClient', StubInvenTreeClient):
            response = self.client.post(self.url('syncmapping-list'), payload, format='json')

        self.assertEqual(response.status_code, 400, response.content)
        codes = [detail.code for detail in response.data['__all__']]
        self.assertIn('incompatible', codes)
        self.assertIn('discovered', str(response.data['__all__']))
        # Nothing was persisted.
        self.assertFalse(SyncMapping.objects.filter(name='Inventory items to parts').exists())

    def test_mapping_write_sets_transform(self):
        payload = self.mapping_payload(field_pairs=[
            {'netbox_field': 'part_id', 'inventree_field': 'IPN'},
            {'netbox_field': 'manufacturer', 'netbox_subfield': 'name', 'inventree_field': 'keywords'},
        ])
        with mock.patch.object(validation, 'InvenTreeClient', StubInvenTreeClient):
            response = self.client.post(self.url('syncmapping-list'), payload, format='json')

        self.assertEqual(response.status_code, 201, response.content)

        # Pairs come back with the transform validate_mapping computed, in the
        # model's deterministic (netbox_field, inventree_field) order.
        pairs = response.json()['field_pairs']
        self.assertEqual(
            [(pair['netbox_field'], pair['transform']) for pair in pairs],
            [('manufacturer', 'str'), ('part_id', 'none')],
        )

        mapping = SyncMapping.objects.get(name='Inventory items to parts')
        self.assertEqual(
            {(p.netbox_field, p.transform) for p in mapping.field_pairs.all()},
            {('manufacturer', 'str'), ('part_id', 'none')},
        )


class SyncNowTestCase(PluginAPITestCase):

    def test_sync_now_enqueues_reconcile(self):
        mapping = SyncMapping.objects.create(
            name='Inventory items to parts',
            connection=self.connection,
            netbox_content_type=self.inventoryitem_ct,
            inventree_endpoint='/api/part/',
            enabled=False,  # disabled: its only ReconcileJob is the one we enqueue
        )
        self.assertFalse(ReconcileJob.get_jobs(mapping).exists())

        response = self.client.post(self.url('syncmapping-sync', pk=mapping.pk))

        self.assertEqual(response.status_code, 202, response.content)
        jobs = ReconcileJob.get_jobs(mapping)
        self.assertEqual(jobs.count(), 1)
        job = jobs.first()
        # One-shot manual sync: pending now, no recurring interval.
        self.assertEqual(job.status, 'pending')
        self.assertIsNone(job.interval)
        self.assertEqual(response.data['job']['id'], job.pk)


class TokenWriteOnlyTestCase(PluginAPITestCase):

    def test_token_write_only(self):
        # List and detail never include the token.
        response = self.client.get(self.url('inventreeinstance-list'))
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()['results']
        self.assertTrue(rows)
        for row in rows:
            self.assertNotIn('api_token', row)

        response = self.client.get(self.url('inventreeinstance-detail', pk=self.connection.pk))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotIn('api_token', response.json())

        # A write accepts it, persists it, and never echoes it back.
        response = self.client.post(self.url('inventreeinstance-list'), {
            'name': 'Secondary InvenTree',
            'server_url': 'http://inventree2.test',
            'api_token': 'another-secret',
            'enabled': False,
        }, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        self.assertNotIn('api_token', response.json())
        self.assertEqual(
            InvenTreeInstance.objects.get(name='Secondary InvenTree').api_token,
            'another-secret',
        )
