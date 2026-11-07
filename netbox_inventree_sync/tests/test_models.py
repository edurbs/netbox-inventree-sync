from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.test import TestCase

from dcim.models import Site

from netbox_inventree_sync.choices import SyncStatusChoices
from netbox_inventree_sync.choices import SyncActionChoices
from netbox_inventree_sync.models import (
    InvenTreeInstance, SyncFieldPair, SyncLogEntry, SyncMapping, SyncedObject,
)


class SyncedObjectModelTestCase(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.instance = InvenTreeInstance.objects.create(
            name='Primary InvenTree',
            url='http://localhost:1337',
            api_token='inv-token',
        )
        cls.mapping = SyncMapping.objects.create(
            name='Sites to Parts',
            connection=cls.instance,
            netbox_content_type=ContentType.objects.get_for_model(Site),
            inventree_endpoint='/api/part/',
        )

    def test_synced_object_unique_per_mapping(self):
        SyncedObject.objects.create(mapping=self.mapping, netbox_object_id=42)

        # Same (mapping, netbox_object_id) must be rejected.
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                SyncedObject.objects.create(mapping=self.mapping, netbox_object_id=42)

        # The constraint is per-mapping: a different mapping may track the same object.
        other = SyncMapping.objects.create(
            name='Sites to Parts (cad)',
            connection=self.instance,
            netbox_content_type=ContentType.objects.get_for_model(Site),
            inventree_endpoint='/api/part/category/',
        )
        twin = SyncedObject.objects.create(mapping=other, netbox_object_id=42)
        self.assertIsNotNone(twin.pk)

    def test_mapping_multi_endpoint_allowed(self):
        duplicate = SyncMapping(
            name=self.mapping.name,
            connection=self.mapping.connection,
            netbox_content_type=self.mapping.netbox_content_type,
            inventree_endpoint='/api/part/category/',  # different endpoint
        )
        duplicate.full_clean()
        duplicate.save()

        self.assertEqual(
            SyncMapping.objects.filter(
                connection=self.mapping.connection,
                netbox_content_type=self.mapping.netbox_content_type,
            ).count(),
            2,
        )


class StringRepresentationTestCase(TestCase):

    def test_str_methods(self):
        instance = InvenTreeInstance(name='Primary InvenTree')
        self.assertEqual(str(instance), 'Primary InvenTree')

        mapping = SyncMapping(name='Sites to Parts')
        self.assertEqual(str(mapping), 'Sites to Parts')


class ChoicesTestCase(TestCase):

    def test_status_choices_values(self):
        self.assertEqual(
            SyncStatusChoices.values,
            ['pending', 'synced', 'failed', 'orphaned'],
        )


class MappingDeletionCascadeTestCase(TestCase):
    """Deleting a mapping removes its own sync state and keeps the log.

    ``SyncedObject.mapping`` is PROTECT so no link row is ever orphaned
    by accident — but an *explicit* mapping delete (UI or REST) is a
    deliberate retirement of the integration and must succeed, cleaning
    up pairs and link rows transactionally. ``SyncLogEntry.synced`` is
    SET_NULL, so history survives detached.
    """

    def test_delete_cascades_sync_state_but_keeps_log(self):
        from django.contrib.contenttypes.models import ContentType
        from dcim.models import Manufacturer

        instance = InvenTreeInstance.objects.create(
            name='c', url='http://x.test', api_token='t'
        )
        mapping = SyncMapping.objects.create(
            name='m',
            connection=instance,
            netbox_content_type=ContentType.objects.get_for_model(Manufacturer),
            inventree_endpoint='/api/company/',
        )
        SyncFieldPair.objects.create(
            mapping=mapping, netbox_field='name', inventree_field='name'
        )
        synced = SyncedObject.objects.create(
            mapping=mapping, netbox_object_id=5, inventree_pk=9,
            status=SyncStatusChoices.SYNCED,
        )
        SyncLogEntry.objects.create(
            synced=synced, action=SyncActionChoices.UPDATE, payload={'name': 'x'}
        )

        mapping.delete()

        self.assertFalse(SyncMapping.objects.filter(pk=mapping.pk).exists())
        self.assertFalse(SyncFieldPair.objects.exists())
        self.assertFalse(SyncedObject.objects.exists())
        log = SyncLogEntry.objects.get()
        self.assertIsNone(log.synced)
