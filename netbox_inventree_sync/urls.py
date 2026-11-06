"""Plugin UI URL configuration.

The REST API needs no wiring here: NetBox discovers a plugin's API URLs
by convention — it imports ``netbox_inventree_sync.api.urls`` and mounts
its ``urlpatterns`` under ``/api/plugins/netbox_inventree_sync/`` (see
``netbox/netbox/plugins/urls.py``). This module carries the plugin's
*UI* views (the connections/mappings CRUD pages); they arrive with the
UI task and will be registered below.
"""

urlpatterns = []
