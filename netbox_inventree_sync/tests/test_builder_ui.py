"""UI tests: the mapping field-pair builder screen.

The InvenTree side is stubbed at the module attribute each consumer
imports — ``views.InvenTreeClient`` for the page's server-side fields
fetch, ``sync.validation.InvenTreeClient`` for the POST's
``validate_mapping`` — the same pattern ``test_api`` and ``test_ui``
use. No HTTP ever leaves the process.
"""

import json
import re
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse

from dcim.models import Device, InventoryItem, Manufacturer, VirtualChassis
from users.models import User

from netbox_inventree_sync import views
from netbox_inventree_sync.models import InvenTreeInstance, SyncFieldPair, SyncMapping
from netbox_inventree_sync.sync import validation
from netbox_inventree_sync.sync.client import InventreeFieldSpec


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
    '/api/company/': [
        _string('name'),
        _related('category', 'contactcategory'),
    ],
}


class StubInvenTreeClient:
    """Stands in for :class:`~netbox_inventree_sync.sync.client.InvenTreeClient`."""

    def __init__(self, url, token, timeout=(10, 30)):
        pass

    def get_field_specs(self, endpoint):
        return list(ENDPOINT_SPECS.get(endpoint, []))


class BuilderTestCase(TestCase):
    """Superuser-authenticated client plus a connection and a mapping to build."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='buildersuper', is_superuser=True)
        cls.connection = InvenTreeInstance.objects.create(
            name='Primary InvenTree',
            url='http://inventree.test',
            api_token='secret-token',
            enabled=True,
        )
        # An enabled manufacturer pairing, so pointer->related targets resolve.
        SyncMapping.objects.create(
            name='Manufacturers to companies',
            connection=cls.connection,
            netbox_content_type=ContentType.objects.get_for_model(Manufacturer),
            inventree_endpoint='/api/company/',
            enabled=True,
        )
        cls.mapping = SyncMapping.objects.create(
            name='Inventory items to parts',
            connection=cls.connection,
            netbox_content_type=ContentType.objects.get_for_model(InventoryItem),
            inventree_endpoint='/api/part/',
            enabled=False,
        )

    def setUp(self):
        self.client.force_login(self.user)
        self.builder_url = reverse(
            'plugins:netbox_inventree_sync:syncmapping_builder', kwargs={'pk': self.mapping.pk},
        )

    def get_builder(self):
        """GET the builder with both client stubs in place."""
        with mock.patch.object(views, 'InvenTreeClient', StubInvenTreeClient):
            return self.client.get(self.builder_url)

    def post_pairs(self, pairs):
        payload = {'pairs': json.dumps(pairs)}
        with mock.patch.object(views, 'InvenTreeClient', StubInvenTreeClient), \
                mock.patch.object(validation, 'InvenTreeClient', StubInvenTreeClient):
            return self.client.post(self.builder_url, payload)

    @staticmethod
    def embedded_fields(response):
        """The fields JSON the page embeds for its progressive enhancement."""
        match = re.search(
            r'<script id="builder-fields" type="application/json">(.*?)</script>',
            response.content.decode(),
            re.DOTALL,
        )
        assert match is not None, 'builder page embeds no fields JSON script block'
        return json.loads(match.group(1))


class BuilderRenderTestCase(BuilderTestCase):

    def test_builder_renders_both_columns(self):
        response = self.get_builder()

        self.assertEqual(response.status_code, 200, response.content)
        html = response.content.decode()
        # Two columns: NetBox side lists its fields (serial), InvenTree side
        # lists the endpoint's (IPN) — each offered in the add-pair selects.
        self.assertContains(response, 'NetBox field')
        self.assertContains(response, 'InvenTree field')
        self.assertIn('Serial Number (serial)', html)  # label (name) in the NetBox select
        self.assertIn('IPN (string)', html)  # name (type) in the InvenTree select

    def test_incompatible_options_disabled(self):
        response = self.get_builder()
        html = response.content.decode()

        # The embedded fields JSON carries the compatibility verdicts:
        # boolean 'discovered' is not accepted by string 'IPN' (text
        # 'serial' is), so the client-side greying has a source of truth.
        fields = self.embedded_fields(response)
        inventree_specs = {spec['name']: spec for spec in fields['inventree']}
        self.assertIn('serial', inventree_specs['IPN']['compatible_netbox_fields'])
        self.assertNotIn('discovered', inventree_specs['IPN']['compatible_netbox_fields'])

        # The rendered InvenTree select marks options incompatible with the
        # selected NetBox field disabled, with the type-mismatch tooltip.
        # 'active' (boolean) accepts nothing, so it is disabled whichever
        # text field starts out selected.
        active_option = re.search(r'<option[^>]*value="active"[^>]*>', html)
        assert active_option is not None, 'InvenTree select offers no active option'
        self.assertIn('disabled', active_option.group(0))
        self.assertIn('type mismatch: text \u2192 boolean', active_option.group(0))


class BuilderSaveTestCase(BuilderTestCase):

    def test_save_pairs_valid(self):
        response = self.post_pairs([
            {'netbox_field': 'part_id', 'netbox_subfield': '', 'inventree_field': 'IPN'},
            {'netbox_field': 'manufacturer', 'netbox_subfield': 'name', 'inventree_field': 'keywords'},
        ])

        # Success redirects back to the builder (with a message).
        self.assertEqual(response.status_code, 302, response.content)
        self.assertEqual(response['Location'], self.builder_url)

        pairs = SyncFieldPair.objects.filter(mapping=self.mapping)
        self.assertEqual(
            {(p.netbox_field, p.netbox_subfield, p.inventree_field, p.transform) for p in pairs},
            {('part_id', '', 'IPN', 'none'), ('manufacturer', 'name', 'keywords', 'str')},
        )

    def test_save_pairs_replaces_existing_pairs(self):
        SyncFieldPair.objects.create(
            mapping=self.mapping,
            netbox_field='name',
            inventree_field='description',
            transform='none',
        )

        response = self.post_pairs([
            {'netbox_field': 'serial', 'netbox_subfield': '', 'inventree_field': 'IPN'},
        ])

        self.assertEqual(response.status_code, 302, response.content)
        # A save is a full replace, mirroring the REST serializer semantics.
        pairs = SyncFieldPair.objects.filter(mapping=self.mapping)
        self.assertEqual(
            [(p.netbox_field, p.inventree_field, p.transform) for p in pairs],
            [('serial', 'IPN', 'none')],
        )

    def test_save_pairs_invalid_400(self):
        # boolean -> string violates the compatibility matrix.
        response = self.post_pairs([
            {'netbox_field': 'discovered', 'netbox_subfield': '', 'inventree_field': 'description'},
        ])

        self.assertEqual(response.status_code, 400, response.content)
        # Zero rows were created, and the error names the offending pair.
        self.assertEqual(SyncFieldPair.objects.filter(mapping=self.mapping).count(), 0)
        self.assertContains(response, 'discovered', status_code=400)
        self.assertContains(response, 'not compatible', status_code=400)

        # A second, valid save still works after a rejected one.
        response = self.post_pairs([
            {'netbox_field': 'serial', 'netbox_subfield': '', 'inventree_field': 'IPN'},
        ])
        self.assertEqual(response.status_code, 302, response.content)
        self.assertEqual(SyncFieldPair.objects.filter(mapping=self.mapping).count(), 1)

    def test_save_pairs_closing_a_cycle_400(self):
        """A pair set that closes a mutual pairing cycle is rejected inline.

        The mapping under edit is an ENABLED Device -> /api/part/ mapping;
        the connection already carries an enabled VirtualChassis mapping
        whose master pair points at dcim.device. Saving the device side's
        virtual_chassis pair would make each mapping the other's pairing.
        """
        chassis_mapping = SyncMapping.objects.create(
            name='Chassis to companies',
            connection=self.connection,
            netbox_content_type=ContentType.objects.get_for_model(VirtualChassis),
            inventree_endpoint='/api/company/',
            enabled=True,
        )
        SyncFieldPair.objects.create(
            mapping=chassis_mapping, netbox_field='master', inventree_field='category',
        )
        device_mapping = SyncMapping.objects.create(
            name='Devices to parts',
            connection=self.connection,
            netbox_content_type=ContentType.objects.get_for_model(Device),
            inventree_endpoint='/api/part/',
            enabled=True,
        )
        builder_url = reverse(
            'plugins:netbox_inventree_sync:syncmapping_builder', kwargs={'pk': device_mapping.pk},
        )

        payload = {'pairs': json.dumps([
            {'netbox_field': 'virtual_chassis', 'netbox_subfield': '', 'inventree_field': 'category'},
        ])}
        with mock.patch.object(views, 'InvenTreeClient', StubInvenTreeClient), \
                mock.patch.object(validation, 'InvenTreeClient', StubInvenTreeClient):
            response = self.client.post(builder_url, payload)

        self.assertEqual(response.status_code, 400, response.content)
        # The inline error names the cycle; zero rows were written.
        self.assertContains(response, 'cycle', status_code=400)
        self.assertContains(response, 'Chassis to companies', status_code=400)
        self.assertEqual(SyncFieldPair.objects.filter(mapping=device_mapping).count(), 0)


class BuilderDetailLinkTestCase(BuilderTestCase):

    def test_mapping_detail_links_to_builder(self):
        response = self.client.get(self.mapping.get_absolute_url())

        self.assertEqual(response.status_code, 200, response.content)
        self.assertContains(response, self.builder_url)
