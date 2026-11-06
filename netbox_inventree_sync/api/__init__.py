"""REST API components for the InvenTree sync plugin.

NetBox discovers a plugin's REST API by convention: it imports
``netbox_inventree_sync.api.urls`` and mounts its ``urlpatterns`` under
``/api/plugins/netbox_inventree_sync/`` (see
``netbox/plugins/urls.py``). The submodules here follow NetBox's
recommended plugin API layout (``docs/plugins/development/rest-api.md``).
"""
