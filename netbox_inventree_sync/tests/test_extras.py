"""Tests for the plugin's remaining tutorial surfaces (steps 11-12) + starters.

GraphQL registration is asserted against the assembled schema: importing
``netbox.graphql.schema`` loads every installed plugin's ``graphql.schema``
resource (the mechanism documented for NetBox 4.7 plugin GraphQL) and
``schema.as_str()`` renders the SDL, so a type block in that text proves the
full wiring (query mixin -> plugin schema resource -> strawberry type).
Search registration is asserted against ``registry['search']``, which
``PluginConfig.ready()`` populates by importing the plugin's ``search.py``
module (the ``@register_search`` decorators do the registering).

The seed-command tests follow the established stub pattern from
``test_api``/``test_builder_ui``: ``validation.InvenTreeClient`` is replaced
with a stand-in serving canned OPTIONS specs, so ``--validate`` exercises the
real ``validate_mapping`` path without HTTP. The default-mode test installs
no stub at all — any attempt to contact InvenTree would fail loudly.
"""

import re
from io import StringIO
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from dcim.models import InventoryItem, Manufacturer

from netbox_inventree_sync.models import InvenTreeInstance, SyncFieldPair, SyncMapping
from netbox_inventree_sync.sync import validation
from netbox_inventree_sync.sync.client import InventreeFieldSpec

# Expected starter set: (content type label, endpoint, [(netbox, sub, inventree), ...])
STARTER_SET = [
    (
        'dcim.inventoryitem',
        '/api/part/',
        [
            ('name', '', 'name'),
            ('part_id', '', 'IPN'),
            ('description', '', 'description'),
            ('manufacturer', 'name', 'keywords'),
        ],
    ),
    (
        'dcim.manufacturer',
        '/api/company/',
        [
            ('name', '', 'name'),
            ('description', '', 'description'),
        ],
    ),
    (
        'dcim.inventoryitemrole',
        '/api/part/category/',
        [
            ('name', '', 'name'),
        ],
    ),
]


def _string(name):
    return InventreeFieldSpec(name=name, type='string', required=False, max_length=None, related_model=None)


def _related(name, model):
    return InventreeFieldSpec(name=name, type='related', required=False, max_length=None, related_model=model)


# Specs that make every starter pair valid (text->string, pointer+sub->string).
COMPATIBLE_SPECS = {
    '/api/part/': [
        _string('name'),
        _string('IPN'),
        _string('description'),
        _string('keywords'),
    ],
    '/api/company/': [
        _string('name'),
        _string('description'),
    ],
    '/api/part/category/': [
        _string('name'),
    ],
}

# /api/part/ rejects name->name: NetBox 'name' is text, a 'related' target
# only accepts pointer pairs, so validate_mapping flags the pair incompatible.
INCOMPATIBLE_SPECS = {
    '/api/part/': [
        _related('name', 'partcategory'),
        _string('IPN'),
        _string('description'),
        _string('keywords'),
    ],
    '/api/company/': COMPATIBLE_SPECS['/api/company/'],
    '/api/part/category/': COMPATIBLE_SPECS['/api/part/category/'],
}


def stub_client_class(endpoint_specs):
    """Build an InvenTreeClient stand-in serving canned per-endpoint specs."""

    class _StubInvenTreeClient:
        def __init__(self, url, token, timeout=(10, 30)):
            pass

        def get_field_specs(self, endpoint):
            return list(endpoint_specs.get(endpoint, []))

    return _StubInvenTreeClient


class SeedStarterMappingsTestCase(TestCase):
    """Default seed mode: three disabled mappings, exact pair set, idempotent."""

    @classmethod
    def setUpTestData(cls):
        cls.connection = InvenTreeInstance.objects.create(
            name='Primary InvenTree',
            url='http://inventree.test',
            api_token='inv-token',
        )

    def pairs_of(self, mapping):
        return {
            (pair.netbox_field, pair.netbox_subfield, pair.inventree_field): pair.transform
            for pair in mapping.field_pairs.all()
        }

    def test_seed_creates_three_disabled_mappings(self):
        output = StringIO()
        # No stub installed: the default path must not contact InvenTree at all.
        call_command('seed_starter_mappings', connection=self.connection.pk, stdout=output)

        mappings = SyncMapping.objects.filter(connection=self.connection)
        self.assertEqual(mappings.count(), 3)
        self.assertEqual(mappings.filter(enabled=False).count(), 3)

        expected_transforms = {
            ('name', '', 'name'): 'none',
            ('part_id', '', 'IPN'): 'none',
            ('description', '', 'description'): 'none',
            ('manufacturer', 'name', 'keywords'): 'str',
        }
        for content_type_label, endpoint, expected_pairs in STARTER_SET:
            app_label, _, model = content_type_label.partition('.')
            mapping = mappings.get(
                netbox_content_type=ContentType.objects.get(app_label=app_label, model=model),
                inventree_endpoint=endpoint,
            )
            pairs = self.pairs_of(mapping)
            self.assertEqual(
                {pair_key for pair_key in pairs},
                set(expected_pairs),
                f'unexpected pair set on {content_type_label} -> {endpoint}',
            )
            for pair_key, transform in expected_transforms.items():
                if pair_key in pairs:
                    self.assertEqual(pairs[pair_key], transform, f'{pair_key} transform')

        # The pointer-as-text pair specifically carries the 'str' transform.
        part_mapping = mappings.get(
            netbox_content_type=ContentType.objects.get_for_model(InventoryItem),
            inventree_endpoint='/api/part/',
        )
        self.assertEqual(
            self.pairs_of(part_mapping)[('manufacturer', 'name', 'keywords')],
            'str',
        )

        self.assertIn('3 created', output.getvalue())
        self.assertIn('without live validation', output.getvalue())

        # Second run (by connection *name*): nothing duplicated, nothing changed.
        rerun = StringIO()
        call_command('seed_starter_mappings', connection='Primary InvenTree', stdout=rerun)

        self.assertEqual(SyncMapping.objects.filter(connection=self.connection).count(), 3)
        self.assertEqual(
            SyncFieldPair.objects.filter(mapping__connection=self.connection).count(), 7,
        )
        self.assertIn('3 skipped', rerun.getvalue())
        self.assertNotIn("Created '", rerun.getvalue())

    def test_seed_rejects_unknown_connection(self):
        with self.assertRaises(CommandError):
            call_command('seed_starter_mappings', connection='no-such-instance', stdout=StringIO())


class SeedStarterMappingsValidationTestCase(TestCase):
    """``--validate`` runs the starter pairs through ``validate_mapping``."""

    @classmethod
    def setUpTestData(cls):
        cls.connection = InvenTreeInstance.objects.create(
            name='Primary InvenTree',
            url='http://inventree.test',
            api_token='inv-token',
        )

    def test_valid_specs_pass_and_seed(self):
        output = StringIO()
        with mock.patch.object(validation, 'InvenTreeClient', stub_client_class(COMPATIBLE_SPECS)):
            call_command(
                'seed_starter_mappings', connection=self.connection.pk, validate=True, stdout=output,
            )

        self.assertEqual(SyncMapping.objects.filter(connection=self.connection).count(), 3)
        mapping = SyncMapping.objects.get(
            connection=self.connection,
            netbox_content_type=ContentType.objects.get_for_model(Manufacturer),
            inventree_endpoint='/api/company/',
        )
        # Transforms come from validate_mapping's compatibility matrix.
        pairs = {
            pair.netbox_field: pair.transform for pair in mapping.field_pairs.all()
        }
        self.assertEqual(pairs, {'name': 'none', 'description': 'none'})
        self.assertNotIn('without live validation', output.getvalue())

    def test_incompatible_specs_fail_loudly_and_seed_nothing(self):
        with mock.patch.object(validation, 'InvenTreeClient', stub_client_class(INCOMPATIBLE_SPECS)):
            with self.assertRaises(CommandError) as context:
                call_command(
                    'seed_starter_mappings', connection=self.connection.pk, validate=True, stdout=StringIO(),
                )

        self.assertIn('Inventory Item', str(context.exception))
        # The whole seed is one transaction: a validation failure leaves no rows.
        self.assertEqual(SyncMapping.objects.filter(connection=self.connection).count(), 0)
        self.assertEqual(SyncFieldPair.objects.filter(mapping__connection=self.connection).count(), 0)


class SearchIndexesTestCase(TestCase):

    def test_search_indexes_registered(self):
        from netbox.registry import registry

        instance_index = registry['search'].get('netbox_inventree_sync.inventreeinstance')
        mapping_index = registry['search'].get('netbox_inventree_sync.syncmapping')

        self.assertIsNotNone(instance_index, 'InvenTreeInstance has no search index')
        self.assertIsNotNone(mapping_index, 'SyncMapping has no search index')
        self.assertEqual(
            {name for name, weight in instance_index.fields}, {'name', 'url'},
        )
        self.assertEqual(
            {name for name, weight in mapping_index.fields},
            {'name', 'description', 'inventree_endpoint'},
        )


class GraphQLSchemaTestCase(TestCase):

    def test_graphql_schema_lists_connection_type(self):
        # Importing the schema module assembles it, which loads every
        # plugin's graphql.schema resource — the verified 4.7 mechanism.
        from netbox.graphql.schema import schema

        schema_text = schema.as_str()

        instance_type = re.search(r'\ntype InvenTreeInstanceType \{.*?\n\}', schema_text, re.DOTALL)
        self.assertIsNotNone(instance_type, 'InvenTreeInstanceType missing from GraphQL schema')
        self.assertIn('name', instance_type.group(0))

        # The secret never reaches the GraphQL surface.
        self.assertNotIn('api_token', schema_text)

        # The mapping type (and its query roots) are wired too.
        self.assertIsNotNone(
            re.search(r'\ntype SyncMappingType \{.*?\n\}', schema_text, re.DOTALL),
        )
        # Query fields are indented inside `type Query { ... }`, so anchor on
        # word boundaries rather than line starts.
        self.assertIsNotNone(
            re.search(r'\binventreeinstance_list\b', schema_text),
            'inventreeinstance_list query root missing',
        )
        self.assertIsNotNone(
            re.search(r'\bsyncmapping_list\b', schema_text),
            'syncmapping_list query root missing',
        )
