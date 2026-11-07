from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from dcim.models import InventoryItem
from extras.choices import CustomFieldTypeChoices
from extras.models import CustomField

from netbox_inventree_sync.sync.fields import get_netbox_fields


class NetboxFieldIntrospectionTestCase(TestCase):
    """Introspection of ``dcim.inventoryitem`` (probe-verified on NetBox 4.7.4)."""

    @classmethod
    def setUpTestData(cls):
        cls.content_type = ContentType.objects.get_for_model(InventoryItem)

    def specs_by_name(self):
        return {spec.name: spec for spec in get_netbox_fields(self.content_type)}

    def test_inventoryitem_fields(self):
        specs = get_netbox_fields(self.content_type)
        by_name = {spec.name: spec for spec in specs}

        # Scalar fields probe-verified on dcim.InventoryItem.
        self.assertEqual(by_name['name'].type, 'text')
        self.assertFalse(by_name['name'].custom)
        self.assertEqual(by_name['serial'].type, 'text')
        self.assertEqual(by_name['part_id'].type, 'text')
        self.assertEqual(by_name['discovered'].type, 'boolean')

        # ForeignKey introspected as a pointer to '<app>.<model>'.
        manufacturer = by_name['manufacturer']
        self.assertEqual(manufacturer.type, 'pointer')
        self.assertEqual(manufacturer.pointer_model, 'dcim.manufacturer')
        self.assertEqual(manufacturer.label, 'Manufacturer')

        # Auto/internal columns are never offered for mapping.
        for excluded in ('id', 'created', 'last_updated', 'custom_field_data'):
            self.assertNotIn(excluded, by_name)

        # Ordering is deterministic (stable for later UI/API consumers).
        self.assertEqual(specs, get_netbox_fields(self.content_type))

    def test_manufacturer_pointer_subfields(self):
        by_name = self.specs_by_name()

        # dcim.Manufacturer non-nullable CharFields: description, name, slug;
        # 'name' leads, the remainder stays sorted.
        self.assertEqual(
            by_name['manufacturer'].subfield_options,
            ['name', 'description', 'slug'],
        )

        # The self-pointer 'parent' has 6 candidates (description, label,
        # name, part_id, serial, status): capped at 5, 'name' first.
        self.assertEqual(
            by_name['parent'].subfield_options,
            ['name', 'description', 'label', 'part_id', 'serial'],
        )

    def test_custom_fields_included(self):
        custom_field = CustomField.objects.create(
            name='warranty_vendor',
            type=CustomFieldTypeChoices.TYPE_TEXT,
        )
        custom_field.object_types.set([self.content_type])
        self.addCleanup(custom_field.delete)

        by_name = self.specs_by_name()
        self.assertIn('warranty_vendor', by_name)
        spec = by_name['warranty_vendor']
        self.assertTrue(spec.custom)
        self.assertEqual(spec.type, 'text')
        self.assertIsNone(spec.pointer_model)
        self.assertEqual(spec.subfield_options, [])

    def test_excluded_custom_field_types_absent(self):
        # json/multiselect/multiobject custom fields are outside the v1
        # compatibility boundary: they must not be offered as mappable.
        json_field = CustomField.objects.create(
            name='probe_metadata',
            type=CustomFieldTypeChoices.TYPE_JSON,
        )
        json_field.object_types.set([self.content_type])
        self.addCleanup(json_field.delete)

        multiselect_field = CustomField.objects.create(
            name='probe_regions',
            type=CustomFieldTypeChoices.TYPE_MULTISELECT,
        )
        multiselect_field.object_types.set([self.content_type])
        self.addCleanup(multiselect_field.delete)

        by_name = self.specs_by_name()
        self.assertNotIn('probe_metadata', by_name)
        self.assertNotIn('probe_regions', by_name)

    def test_tags_text_spec(self):
        by_name = self.specs_by_name()

        self.assertIn('tags', by_name)
        spec = by_name['tags']
        self.assertEqual(spec.type, 'text')
        self.assertFalse(spec.custom)
        self.assertIsNone(spec.pointer_model)
