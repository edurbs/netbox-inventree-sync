"""UI tests: connections & mappings CRUD screens and the nav menu.

These exercise the tutorial-style generic views (list/add/edit) through
Django's test client with a logged-in superuser. The InvenTree side of
the "Test connection" button is stubbed at ``views.InvenTreeClient`` —
the same module-attribute patching pattern the API tests use — so no
HTTP ever leaves the process.
"""

from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse

from dcim.models import InventoryItem
from users.models import User

from netbox_inventree_sync import views
from netbox_inventree_sync.forms import SyncMappingForm
from netbox_inventree_sync.models import InvenTreeInstance, SyncMapping
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


class MappingUITestCase(PluginUITestCase):

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


class NavMenuUITestCase(PluginUITestCase):

    def test_nav_menu_present(self):
        response = self.client.get(reverse('plugins:netbox_inventree_sync:inventreeinstance_list'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'InvenTree Sync')
        self.assertContains(response, 'Connections')
        self.assertContains(response, 'Mappings')
