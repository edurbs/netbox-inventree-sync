"""Tests for mapping validation (compatibility, duplicates, pairings, cycles).

`validate_mapping` is exercised against real NetBox field introspection of
``dcim.inventoryitem`` with the InvenTree side stubbed: ``StubInvenTreeClient``
serves canned per-endpoint OPTIONS specs and replaces the module attribute
``validation.InvenTreeClient``, so no HTTP ever happens. The cycle tests use
``dcim.device``/``dcim.virtualchassis`` — a real pair of models that
mutually reference each other (``Device.virtual_chassis`` and
``VirtualChassis.master``).
"""

from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.test import TestCase

from dcim.models import (
    Device, InventoryItem, InventoryItemRole, Manufacturer, Region, VirtualChassis,
)

from netbox_inventree_sync.models import InvenTreeInstance, SyncFieldPair, SyncMapping
from netbox_inventree_sync.sync import validation
from netbox_inventree_sync.sync.client import InventreeFieldSpec
from netbox_inventree_sync.sync.validation import (
    validate_mapping,
    validate_mapping_graph,
    validate_mapping_graph_for_write,
)


def _string(name):
    return InventreeFieldSpec(name=name, type='string', required=False, max_length=None, related_model=None)


def _related(name, model):
    return InventreeFieldSpec(name=name, type='related', required=False, max_length=None, related_model=model)


# Minimal spec tables per endpoint, shaped like InvenTreeClient.get_field_specs output.
ENDPOINT_SPECS = {
    '/api/part/': [
        _string('IPN'),
        _string('keywords'),
        _string('description'),
        _related('category', 'partcategory'),
    ],
    '/api/company/': [
        _string('name'),
        _related('category', 'contactcategory'),
    ],
    '/api/part/category/': [
        _string('name'),
        _related('parent', 'partcategory'),
    ],
}


class StubInvenTreeClient:
    """Stands in for :class:`~netbox_inventree_sync.sync.client.InvenTreeClient`."""

    def __init__(self, url, token, timeout=(10, 30)):
        pass

    def get_field_specs(self, endpoint):
        return list(ENDPOINT_SPECS.get(endpoint, []))


class ValidateMappingTestCase(TestCase):
    """``validate_mapping`` over ``dcim.inventoryitem`` with the InvenTree side stubbed."""

    @classmethod
    def setUpTestData(cls):
        cls.connection = InvenTreeInstance.objects.create(
            name='Primary InvenTree',
            url='http://inventree.test',
            api_token='inv-token',
        )
        cls.part_mapping = SyncMapping.objects.create(
            name='Inventory items to parts',
            connection=cls.connection,
            netbox_content_type=ContentType.objects.get_for_model(InventoryItem),
            inventree_endpoint='/api/part/',
            enabled=True,
        )
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
        patcher = mock.patch.object(validation, 'InvenTreeClient', StubInvenTreeClient)
        patcher.start()
        self.addCleanup(patcher.stop)

    def pair(self, **kwargs):
        defaults = dict(mapping=self.part_mapping, netbox_field='part_id', inventree_field='IPN')
        defaults.update(kwargs)
        return SyncFieldPair(**defaults)

    def error_codes(self, context):
        return {error.code for error in context.exception.error_list}

    def test_valid_pairs_returned_with_transforms(self):
        pairs = [
            self.pair(netbox_field='part_id', inventree_field='IPN'),
            self.pair(netbox_field='manufacturer', netbox_subfield='name', inventree_field='keywords'),
        ]

        result = validate_mapping(self.part_mapping, pairs)

        self.assertEqual([pair.transform for pair in result], ['none', 'str'])

    def test_incompatible_pair_raises(self):
        pairs = [self.pair(netbox_field='discovered', inventree_field='description')]

        with self.assertRaises(ValidationError) as ctx:
            validate_mapping(self.part_mapping, pairs)

        self.assertEqual(self.error_codes(ctx), {'incompatible'})
        self.assertIn('discovered', str(ctx.exception))

    def test_duplicate_inventree_target_raises(self):
        pairs = [
            self.pair(netbox_field='part_id', inventree_field='description'),
            self.pair(netbox_field='serial', inventree_field='description'),
        ]

        with self.assertRaises(ValidationError) as ctx:
            validate_mapping(self.part_mapping, pairs)

        self.assertEqual(self.error_codes(ctx), {'duplicate_target'})
        self.assertIn('description', str(ctx.exception))

    def test_pairing_missing_when_pairing_mapping_disabled(self):
        pairs = [self.pair(netbox_field='role', inventree_field='category')]

        # The role mapping exists on the connection but is disabled, so
        # dcim.inventoryitemrole is not an available pairing.
        with self.assertRaises(ValidationError) as ctx:
            validate_mapping(self.part_mapping, pairs)

        self.assertEqual(self.error_codes(ctx), {'pairing_missing'})
        self.assertIn('role', str(ctx.exception))

        # Enabling the pairing mapping makes the pair valid again.
        self.role_mapping.enabled = True
        self.role_mapping.save()

        result = validate_mapping(self.part_mapping, pairs)
        self.assertEqual(result[0].transform, 'none')

    def test_pairing_ambiguous_with_two_enabled_pairing_mappings(self):
        # A second enabled mapping of dcim.manufacturer (the unique
        # constraint allows the same content type on different endpoints
        # — the Part + StockItem case): a pointer to it can no longer be
        # resolved through exactly one pairing, so validation must say so
        # and name both candidates instead of letting the engine guess.
        SyncMapping.objects.create(
            name='Manufacturers to suppliers',
            connection=self.connection,
            netbox_content_type=ContentType.objects.get_for_model(Manufacturer),
            inventree_endpoint='/api/supplier/',
            enabled=True,
        )
        pairs = [self.pair(netbox_field='manufacturer', inventree_field='category')]

        with self.assertRaises(ValidationError) as ctx:
            validate_mapping(self.part_mapping, pairs)

        self.assertEqual(self.error_codes(ctx), {'pairing_ambiguous'})
        message = str(ctx.exception)
        self.assertIn('Manufacturers to companies', message)
        self.assertIn('Manufacturers to suppliers', message)

        # Disabling one of the two candidates resolves the ambiguity.
        self.manufacturer_mapping.enabled = False
        self.manufacturer_mapping.save()
        result = validate_mapping(self.part_mapping, pairs)
        self.assertEqual(result[0].transform, 'none')

    def test_subfield_required_for_pointer_to_string(self):
        pairs = [self.pair(netbox_field='manufacturer', netbox_subfield='', inventree_field='keywords')]

        with self.assertRaises(ValidationError) as ctx:
            validate_mapping(self.part_mapping, pairs)

        self.assertEqual(self.error_codes(ctx), {'subfield_required'})
        self.assertIn('manufacturer', str(ctx.exception))

    def test_unknown_field_raises(self):
        pairs = [
            self.pair(netbox_field='nonexistent', inventree_field='IPN'),
            self.pair(netbox_field='part_id', inventree_field='nonexistent'),
        ]

        with self.assertRaises(ValidationError) as ctx:
            validate_mapping(self.part_mapping, pairs)

        self.assertEqual(self.error_codes(ctx), {'unknown_field'})
        self.assertIn('nonexistent', str(ctx.exception))


class ValidateMappingGraphTestCase(TestCase):
    """``validate_mapping_graph`` walks pairing dependencies between enabled mappings."""

    @classmethod
    def setUpTestData(cls):
        cls.connection = InvenTreeInstance.objects.create(
            name='Primary InvenTree',
            url='http://inventree.test',
            api_token='inv-token',
        )
        cls.device_mapping = SyncMapping.objects.create(
            name='Devices to parts',
            connection=cls.connection,
            netbox_content_type=ContentType.objects.get_for_model(Device),
            inventree_endpoint='/api/part/',
            enabled=True,
        )
        cls.chassis_mapping = SyncMapping.objects.create(
            name='Chassis to companies',
            connection=cls.connection,
            netbox_content_type=ContentType.objects.get_for_model(VirtualChassis),
            inventree_endpoint='/api/company/',
            enabled=True,
        )
        # Device.virtual_chassis points at dcim.virtualchassis (the chassis
        # mapping's content type) and VirtualChassis.master points back at
        # dcim.device (the device mapping's content type): a mutual pairing.
        SyncFieldPair.objects.create(
            mapping=cls.device_mapping,
            netbox_field='virtual_chassis',
            inventree_field='category',
        )
        SyncFieldPair.objects.create(
            mapping=cls.chassis_mapping,
            netbox_field='master',
            inventree_field='category',
        )

    def setUp(self):
        patcher = mock.patch.object(validation, 'InvenTreeClient', StubInvenTreeClient)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cycle_between_mutually_referencing_mappings(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_mapping_graph([self.device_mapping, self.chassis_mapping])

        self.assertEqual(ctx.exception.error_list[0].code, 'cycle')
        message = str(ctx.exception)
        self.assertIn(self.device_mapping.name, message)
        self.assertIn(self.chassis_mapping.name, message)

    def test_disabled_mappings_are_not_graph_nodes(self):
        self.chassis_mapping.enabled = False
        self.chassis_mapping.save()

        # With the chassis mapping disabled there is no pairing node for
        # dcim.virtualchassis, hence no edge and no cycle.
        validate_mapping_graph([self.device_mapping, self.chassis_mapping])

    def test_graph_walk_builds_one_client_per_connection(self):
        # The whole walk of one connection's mappings shares ONE client,
        # so its OPTIONS cache serves each endpoint once however many
        # mappings target it.
        built = []

        class CountingClient(StubInvenTreeClient):
            def __init__(self, url, token, timeout=(10, 30)):
                built.append((url, token))

        with mock.patch.object(validation, 'InvenTreeClient', CountingClient):
            with self.assertRaises(ValidationError):
                validate_mapping_graph([self.device_mapping, self.chassis_mapping])

        self.assertEqual(len(built), 1)


class ValidateMappingGraphForWriteTestCase(TestCase):
    """The write-path wrapper: a pending pair set and enable flips count."""

    @classmethod
    def setUpTestData(cls):
        cls.connection = InvenTreeInstance.objects.create(
            name='Primary InvenTree',
            url='http://inventree.test',
            api_token='inv-token',
        )
        cls.chassis_mapping = SyncMapping.objects.create(
            name='Chassis to companies',
            connection=cls.connection,
            netbox_content_type=ContentType.objects.get_for_model(VirtualChassis),
            inventree_endpoint='/api/company/',
            enabled=True,
        )
        # VirtualChassis.master points at dcim.device: the chassis side of
        # what would be a mutual cycle with a device mapping.
        SyncFieldPair.objects.create(
            mapping=cls.chassis_mapping,
            netbox_field='master',
            inventree_field='category',
        )

    def setUp(self):
        patcher = mock.patch.object(validation, 'InvenTreeClient', StubInvenTreeClient)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_pending_pair_set_closing_a_cycle_is_rejected(self):
        # The device mapping under write has no rows yet; its POSTed pair
        # (Device.virtual_chassis -> related) is what would close the
        # cycle, so the graph check must see the PENDING pairs, not the
        # (empty) stored set.
        device_mapping = SyncMapping(
            name='Devices to parts',
            connection=self.connection,
            netbox_content_type=ContentType.objects.get_for_model(Device),
            inventree_endpoint='/api/part/',
            enabled=True,
        )
        pairs = [SyncFieldPair(
            mapping=device_mapping,
            netbox_field='virtual_chassis',
            inventree_field='category',
        )]

        with self.assertRaises(ValidationError) as ctx:
            validate_mapping_graph_for_write(device_mapping, pairs)

        self.assertEqual(ctx.exception.error_list[0].code, 'cycle')
        self.assertIn('Chassis to companies', str(ctx.exception))

    def test_enabling_self_referencing_mapping_is_rejected(self):
        # Region.parent points at dcim.region: a region mapping with a
        # parent->related pair is a self-cycle the moment it is enabled.
        # Stored here while disabled (no graph check applies to disabled
        # mappings); the enable flip is a write like any other.
        region_mapping = SyncMapping.objects.create(
            name='Regions to categories',
            connection=self.connection,
            netbox_content_type=ContentType.objects.get_for_model(Region),
            inventree_endpoint='/api/part/category/',
            enabled=False,
        )
        SyncFieldPair.objects.create(
            mapping=region_mapping,
            netbox_field='parent',
            inventree_field='parent',
        )
        region_mapping.enabled = True  # the write's provisional state

        with self.assertRaises(ValidationError) as ctx:
            validate_mapping_graph_for_write(region_mapping, None)

        self.assertEqual(ctx.exception.error_list[0].code, 'cycle')

    def test_acyclic_write_passes(self):
        device_mapping = SyncMapping(
            name='Devices to parts',
            connection=self.connection,
            netbox_content_type=ContentType.objects.get_for_model(Device),
            inventree_endpoint='/api/part/',
            enabled=True,
        )
        pairs = [SyncFieldPair(
            mapping=device_mapping, netbox_field='serial', inventree_field='IPN',
        )]

        # serial is a plain text pair: no edge, no cycle.
        validate_mapping_graph_for_write(device_mapping, pairs)
