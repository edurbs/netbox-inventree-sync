"""Router registration for the plugin's REST API.

NetBox auto-discovers this module (``netbox/plugins/urls.py`` imports
``<plugin>.api.urls`` and mounts its ``urlpatterns`` under
``/api/plugins/netbox_inventree_sync/``), so nothing needs to reference
it from the plugin's UI ``urls.py``.
"""

from django.urls import path
from netbox.api.routers import NetBoxRouter

from . import views

app_name = 'netbox_inventree_sync-api'

router = NetBoxRouter()
router.register('connections', views.InvenTreeInstanceViewSet)
router.register('mappings', views.SyncMappingViewSet)
router.register('synced-objects', views.SyncedObjectViewSet)
router.register('sync-log', views.SyncLogEntryViewSet)

urlpatterns = [
    path('fields/', views.MappableFieldsView.as_view(), name='fields'),
    *router.urls,
]
