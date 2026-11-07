"""UI tests: connections & mappings CRUD screens and the nav menu.

These exercise the tutorial-style generic views (list/add/edit) through
Django's test client with a logged-in superuser. The InvenTree side of
the "Test connection" button is stubbed at ``views.InvenTreeClient`` —
the same module-attribute patching pattern the API tests use — so no
HTTP ever leaves the process. The Task 14 screens (synced-objects /
sync-log lists, the mapping "Sync now" action and the object-detail
panel) are covered here too; none of them talk to InvenTree either.
"""

from unittest import mock

from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Job
from dcim.models import Device, DeviceRole, DeviceType, InventoryItem, Manufacturer, Site
from users.models import User

from netbox_inventree_sync import views
from netbox_inventree_sync.choices import SyncActionChoices, SyncStatusChoices
from netbox_inventree_sync.forms import SyncMappingForm
from netbox_inventree_sync.jobs import ReconcileJob
from netbox_inventree_sync.models import (
    InvenTreeInstance, SyncFieldPair, SyncLogEntry, SyncMapping, SyncedObject,
)
from netbox_inventree_sync.sync.client import InvenTreeClientError


class PluginUITestCase(TestCase):
    """Superuser-authenticated client plus one InvenTree connection."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='uisuper', is_superuser=True)
        cls.connection = InvenTreeInstance.objects.create(
            name='Primary InvenTree',
            url='http://inventree.test',
            api_token='secret-token',
            enabled=True,
        )

    def setUp(self):
        self.client.force_login(self.user)


class ConnectionUITestCase(PluginUITestCase):

    def test_connections_list_renders(self):
        response = self.client.get(reverse('plugins:netbox_inventree_sync:inventreeinstance_list'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Primary InvenTree')

    def test_connection_create_and_test_button(self):
        add_url = reverse('plugins:netbox_inventree_sync:inventreeinstance_add')

        # A valid create POST persists the object and redirects.
        response = self.client.post(add_url, {
            'name': 'Secondary InvenTree',
            'url': 'http://inventree2.test',
            'api_token': 'token-2',
            'enabled': 'on',
        })
        self.assertEqual(response.status_code, 302, response.content)
        instance = InvenTreeInstance.objects.get(name='Secondary InvenTree')
        self.assertEqual(instance.api_token, 'token-2')

        # The edit form offers the Test connection submit and never
        # echoes the stored token back into the HTML.
        edit_url = reverse('plugins:netbox_inventree_sync:inventreeinstance_edit', kwargs={'pk': instance.pk})
        response = self.client.get(edit_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Test connection')
        self.assertNotContains(response, 'token-2')

        # The Test connection action probes InvenTree without saving.
        form_data = {
            'name': 'Should Not Save',
            'url': 'http://inventree2.test',
            'api_token': '',  # blank keeps the stored token
            'enabled': 'on',
            'test_connection': '1',
        }
        stub = mock.Mock(**{'test_connection.return_value': {'user': 'sync-bot', 'version': '9.9.9-test'}})
        with mock.patch.object(views, 'InvenTreeClient', return_value=stub) as factory:
            response = self.client.post(edit_url, form_data)

        self.assertEqual(response.status_code, 200, response.content)
        # A blank token falls back to the stored one; url comes from the form.
        factory.assert_called_once_with('http://inventree2.test', 'token-2')
        self.assertContains(response, 'sync-bot')
        self.assertContains(response, '9.9.9-test')

        instance.refresh_from_db()
        self.assertEqual(instance.name, 'Secondary InvenTree')  # nothing was saved
        self.assertEqual(instance.api_token, 'token-2')
        self.assertEqual(InvenTreeInstance.objects.count(), 2)

        # A failing probe reports the upstream error, still without saving.
        failing = mock.Mock(**{'test_connection.side_effect': InvenTreeClientError(
            'InvenTree API GET /api/ request failed: connection refused',
        )})
        with mock.patch.object(views, 'InvenTreeClient', return_value=failing):
            response = self.client.post(edit_url, form_data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'connection refused')
        instance.refresh_from_db()
        self.assertEqual(instance.name, 'Secondary InvenTree')

    def test_connection_success_without_user_field(self):
        """InvenTree 1.7's GET /api/ carries no top-level user field.

        The success message must degrade gracefully: version shown, no
        literal 'None' interpolated where the username would be.
        """
        instance = InvenTreeInstance.objects.create(
            name='No-User Probe', url='http://inventree3.test', api_token='token-3'
        )
        edit_url = reverse('plugins:netbox_inventree_sync:inventreeinstance_edit', kwargs={'pk': instance.pk})
        stub = mock.Mock(**{'test_connection.return_value': {'version': '1.7.0'}})  # no 'user'
        with mock.patch.object(views, 'InvenTreeClient', return_value=stub):
            response = self.client.post(edit_url, {
                'name': 'No-User Probe',
                'url': 'http://inventree3.test',
                'api_token': '',
                'enabled': 'on',
                'test_connection': '1',
            })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertContains(response, '1.7.0')
        self.assertNotContains(response, 'None')


class MappingUITestCase(PluginUITestCase):

    def test_mapping_enable_via_ui_closes_cycle_rejected(self):
        """Enabling a mapping through the UI edit form runs the cycle gate.

        The REST serializer gates every write that can change the pairing
        graph; the form must not be the loophole: a disabled mapping
        holding reciprocal pointer pairs cannot be switched on from the
        UI any more than it can via the API.
        """
        from dcim.models import Device, VirtualChassis

        chassis_mapping = SyncMapping.objects.create(
            name='UI cycle chassis',
            connection=self.connection,
            netbox_content_type=ContentType.objects.get_for_model(VirtualChassis),
            inventree_endpoint='/api/company/',
            enabled=True,
        )
        SyncFieldPair.objects.create(
            mapping=chassis_mapping, netbox_field='master', inventree_field='category',
        )
        device_mapping = SyncMapping.objects.create(
            name='UI cycle devices',
            connection=self.connection,
            netbox_content_type=ContentType.objects.get_for_model(Device),
            inventree_endpoint='/api/part/',
            enabled=False,  # legal while disabled: disabled nodes aren't checked
        )
        SyncFieldPair.objects.create(
            mapping=device_mapping, netbox_field='virtual_chassis', inventree_field='category',
        )

        edit_url = reverse(
            'plugins:netbox_inventree_sync:syncmapping_edit', kwargs={'pk': device_mapping.pk}
        )
        from unittest import mock as _mock
        from netbox_inventree_sync.sync import validation
        from netbox_inventree_sync.tests.test_api import StubInvenTreeClient
        with _mock.patch.object(validation, 'InvenTreeClient', StubInvenTreeClient):
            response = self.client.post(edit_url, {
                'name': device_mapping.name,
                'connection': self.connection.pk,
                'netbox_content_type': ContentType.objects.get_for_model(Device).pk,
                'inventree_endpoint': '/api/part/',
                'match_netbox_field': '',
                'match_inventree_field': '',
                'reconcile_interval_minutes': 60,
                'enabled': 'on',
                'description': '',
            })

        self.assertEqual(response.status_code, 200, response.content)
        self.assertContains(response, 'cycle')
        device_mapping.refresh_from_db()
        self.assertFalse(device_mapping.enabled)  # rejected, not saved

    def test_mapping_create_renders_content_type_choices(self):
        response = self.client.get(reverse('plugins:netbox_inventree_sync:syncmapping_add'))

        self.assertEqual(response.status_code, 200, response.content)
        # A change-logged NetBox model is offered with its app-labeled name
        # (the human-friendly form of "dcim.inventoryitem").
        self.assertContains(response, 'DCIM &gt; Inventory Item')
        # The plugin's own models are not offered as mapping sources.
        self.assertNotContains(response, 'InvenTree Sync &gt; Sync Mapping')

        # The choices are the change-logged models minus the plugin's own
        # models and Django's internal ones.
        form = SyncMappingForm()
        offered = set(form.fields['netbox_content_type'].queryset.values_list('pk', flat=True))
        self.assertIn(ContentType.objects.get_for_model(InventoryItem).pk, offered)
        self.assertNotIn(ContentType.objects.get_for_model(SyncMapping).pk, offered)
        self.assertNotIn(ContentType.objects.get_for_model(User).pk, offered)

    def test_bulk_delete_cascades_sync_state(self):
        # Bulk-deleting mappings with sync history succeeds and cascades
        # the plugin's own sync state (pairs + link rows); the sync log
        # survives (its synced FK is SET_NULL). Previously the PROTECT on
        # SyncedObject.mapping made any mapping with history undeletable
        # via UI or REST.
        mapping = SyncMapping.objects.create(
            name='Retired mapping',
            connection=self.connection,
            netbox_content_type=ContentType.objects.get_for_model(Manufacturer),
            inventree_endpoint='/api/company/',
            enabled=False,
        )
        SyncFieldPair.objects.create(
            mapping=mapping,
            netbox_field='name',
            inventree_field='name',
            transform='none',
        )
        synced = SyncedObject.objects.create(
            mapping=mapping,
            netbox_object_id=123,
            inventree_pk=7,
            status=SyncStatusChoices.SYNCED,
        )
        SyncLogEntry.objects.create(
            synced=synced,
            action=SyncActionChoices.CREATE,
            payload={'name': 'x'},
        )

        response = self.client.post(
            reverse('plugins:netbox_inventree_sync:syncmapping_bulk_delete'),
            {'pk': [mapping.pk], 'confirm': 'true', '_confirm': 'true'},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(SyncMapping.objects.filter(pk=mapping.pk).exists())
        self.assertFalse(SyncFieldPair.objects.filter(mapping_id=mapping.pk).exists())
        self.assertFalse(SyncedObject.objects.filter(pk=synced.pk).exists())
        log = SyncLogEntry.objects.get()
        self.assertIsNone(log.synced)  # history survives, detached

    def test_bulk_delete_cancels_schedules(self):
        # Bulk-deleting an ENABLED mapping also retires its recurring
        # reconcile schedule — NetBox would otherwise resurrect the job
        # (and its errors) every interval after the mapping is gone.
        from core.choices import JobStatusChoices
        from core.models import Job
        from netbox_inventree_sync.jobs import ReconcileJob

        mapping = SyncMapping.objects.create(
            name='Scheduled retiree',
            connection=self.connection,
            netbox_content_type=ContentType.objects.get_for_model(Site),
            inventree_endpoint='/api/company/',
            enabled=True,
        )
        self.assertTrue(
            Job.objects.filter(
                name=ReconcileJob.name, object_id=mapping.pk,
                status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES,
            ).exists()
        )

        response = self.client.post(
            reverse('plugins:netbox_inventree_sync:syncmapping_bulk_delete'),
            {'pk': [mapping.pk], 'confirm': 'true', '_confirm': 'true'},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            Job.objects.filter(
                name=ReconcileJob.name, object_id=mapping.pk,
                status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES,
            ).exists()
        )


class NavMenuUITestCase(PluginUITestCase):

    def test_nav_menu_present(self):
        response = self.client.get(reverse('plugins:netbox_inventree_sync:inventreeinstance_list'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'InvenTree Sync')
        self.assertContains(response, 'Connections')
        self.assertContains(response, 'Mappings')


class SyncedObjectListUITestCase(PluginUITestCase):
    """The read-only synced-objects list and its status/mapping filters."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        manufacturer_ct = ContentType.objects.get_for_model(Manufacturer)
        # Two mappings (different endpoints: the pair is unique per
        # endpoint), disabled so no recurring reconcile schedule appears.
        cls.mapping_a = SyncMapping.objects.create(
            name='Manufacturers A', connection=cls.connection,
            netbox_content_type=manufacturer_ct, inventree_endpoint='/api/company/',
            enabled=False,
        )
        cls.mapping_b = SyncMapping.objects.create(
            name='Manufacturers B', connection=cls.connection,
            netbox_content_type=manufacturer_ct, inventree_endpoint='/api/supplier/',
            enabled=False,
        )
        cls.synced_ok = SyncedObject.objects.create(
            mapping=cls.mapping_a, netbox_object_id=11, inventree_pk=101,
            status=SyncStatusChoices.SYNCED, last_synced_at=timezone.now(),
        )
        cls.synced_failed = SyncedObject.objects.create(
            mapping=cls.mapping_b, netbox_object_id=22,
            status=SyncStatusChoices.FAILED, last_error='InvenTree API request failed: connection refused',
        )

    def test_synced_objects_list_with_filters(self):
        list_url = reverse('plugins:netbox_inventree_sync:syncedobject_list')

        # Unfiltered: both link rows show, the synced one with its
        # InvenTree counterpart link (<url> + endpoint path + pk) and the
        # failed one with its error text.
        response = self.client.get(list_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'dcim.manufacturer #11')
        self.assertContains(response, 'dcim.manufacturer #22')
        self.assertContains(response, 'http://inventree.test/company/101/')
        self.assertContains(response, 'connection refused')

        # status=failed keeps only the failed row.
        response = self.client.get(list_url, {'status': SyncStatusChoices.FAILED})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'dcim.manufacturer #22')
        self.assertNotContains(response, 'dcim.manufacturer #11')

        # The mapping filter narrows to one mapping's rows.
        response = self.client.get(list_url, {'mapping_id': self.mapping_a.pk})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'dcim.manufacturer #11')
        self.assertNotContains(response, 'dcim.manufacturer #22')


class SyncLogListUITestCase(PluginUITestCase):
    """The read-only sync-log list (payload/response bodies visible)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        mapping = SyncMapping.objects.create(
            name='Manufacturers to companies', connection=cls.connection,
            netbox_content_type=ContentType.objects.get_for_model(Manufacturer),
            inventree_endpoint='/api/company/', enabled=False,
        )
        synced = SyncedObject.objects.create(
            mapping=mapping, netbox_object_id=11, inventree_pk=101,
            status=SyncStatusChoices.SYNCED,
        )
        cls.entry = SyncLogEntry.objects.create(
            synced=synced, action=SyncActionChoices.CREATE,
            payload={'name': 'Acme', 'description': 'synced from NetBox'},
            response_status=201, response_body='{"pk": 101, "name": "Acme"}',
        )

    def test_sync_log_renders_response_body(self):
        list_url = reverse('plugins:netbox_inventree_sync:synclogentry_list')

        response = self.client.get(list_url)
        self.assertEqual(response.status_code, 200)
        # The response body (and the recorded payload) appear on the page,
        # alongside the action badge and the HTTP status. Table cells are
        # HTML-escaped, so the JSON quotes arrive as &quot; entities.
        self.assertContains(response, '&quot;pk&quot;: 101')
        self.assertContains(response, 'synced from NetBox')
        self.assertContains(response, '201')
        self.assertContains(response, 'Create')

        # The action filter excludes non-matching entries.
        response = self.client.get(list_url, {'action': SyncActionChoices.ERROR})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '&quot;pk&quot;: 101')


class MappingSyncNowUITestCase(PluginUITestCase):
    """The "Sync now" button on the mapping detail page."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Disabled: the recurring reconcile schedule must not exist, so
        # any ReconcileJob row found after POSTing is the one-shot sync.
        cls.mapping = SyncMapping.objects.create(
            name='Manufacturers to companies', connection=cls.connection,
            netbox_content_type=ContentType.objects.get_for_model(Manufacturer),
            inventree_endpoint='/api/company/', enabled=False,
        )

    def reconcile_jobs(self):
        return Job.objects.filter(
            name=ReconcileJob.name,
            object_type=ContentType.objects.get_for_model(SyncMapping),
            object_id=self.mapping.pk,
            interval__isnull=True,
        )

    def test_mapping_detail_sync_now_enqueues_reconcile(self):
        sync_url = reverse('plugins:netbox_inventree_sync:syncmapping_sync', kwargs={'pk': self.mapping.pk})
        detail_url = reverse('plugins:netbox_inventree_sync:syncmapping', kwargs={'pk': self.mapping.pk})

        # The button is a POST form on the mapping detail page.
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Sync now')
        self.assertContains(response, sync_url)

        # POST enqueues one immediate ReconcileJob, flashes the success
        # message and redirects back to the mapping detail page.
        response = self.client.post(sync_url, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.redirect_chain), 1)
        self.assertTrue(response.redirect_chain[0][0].endswith(detail_url))
        self.assertContains(response, 'Sync scheduled')
        jobs = self.reconcile_jobs()
        self.assertEqual(jobs.count(), 1)
        self.assertEqual(jobs.first().status, 'pending')

        # Gate: a user with only view permission gets a 403, no job.
        viewer = User.objects.create_user(username='mapviewer')
        viewer.user_permissions.add(Permission.objects.get(codename='view_syncmapping'))
        self.client.force_login(viewer)
        response = self.client.post(sync_url)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.reconcile_jobs().count(), 1)


class ObjectPanelUITestCase(PluginUITestCase):
    """The InvenTree panel on mapped objects' NetBox detail pages."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.local_connection = InvenTreeInstance.objects.create(
            name='Local InvenTree', url='http://localhost:1337',
            api_token='local-token', enabled=True,
        )
        cls.mapping = SyncMapping.objects.create(
            name='Inventory items to parts', connection=cls.local_connection,
            netbox_content_type=ContentType.objects.get_for_model(InventoryItem),
            inventree_endpoint='/api/part/', enabled=True,
        )
        # Minimal chain so the InventoryItem detail page resolves:
        # site → manufacturer → device type → device → inventory item.
        site = Site.objects.create(name='Panel Site', slug='panel-site')
        manufacturer = Manufacturer.objects.create(name='Panel Manufacturer', slug='panel-manufacturer')
        device_type = DeviceType.objects.create(
            manufacturer=manufacturer, model='Panel Model', slug='panel-model',
        )
        device = Device.objects.create(
            device_type=device_type, name='panel-device-1', site=site,
            role=DeviceRole.objects.create(name='Panel Role', slug='panel-role'),
        )
        cls.item = InventoryItem.objects.create(device=device, name='panel-item-1')
        cls.synced = SyncedObject.objects.create(
            mapping=cls.mapping, netbox_object_id=cls.item.pk, inventree_pk=42,
            status=SyncStatusChoices.SYNCED, last_synced_at=timezone.now(),
        )

    def test_object_panel_shows_on_inventoryitem(self):
        response = self.client.get(reverse('dcim:inventoryitem', kwargs={'pk': self.item.pk}))

        self.assertEqual(response.status_code, 200)
        # The panel card names InvenTree, links the remote counterpart
        # (<connection.url> + endpoint-derived path + pk) and to the
        # plugin's synced-objects list.
        self.assertContains(response, 'InvenTree')
        self.assertContains(response, 'http://localhost:1337/part/42/')
        self.assertContains(response, 'Part #42')
        self.assertContains(response, reverse('plugins:netbox_inventree_sync:syncedobject_list'))

        # A model without any enabled mapping gets no panel at all.
        response = self.client.get(reverse('dcim:site', kwargs={'pk': Site.objects.get(slug='panel-site').pk}))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'http://localhost:1337/part/42/')
