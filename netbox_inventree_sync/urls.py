"""Plugin UI URL configuration.

Views are registered on the models with ``register_model_view()`` (see
``views.py``) and wired here with ``get_model_urls()`` — the same
mechanism NetBox's own apps use. It also picks up the feature views
NetBox auto-registers for the models (changelog, jobs), so their tabs
resolve on the detail pages.

The REST API needs no wiring here: NetBox discovers a plugin's API URLs
by convention — it imports ``netbox_inventree_sync.api.urls`` and mounts
its ``urlpatterns`` under ``/api/plugins/netbox_inventree_sync/`` (see
``netbox/netbox/plugins/urls.py``).
"""

from django.urls import include, path

# Importing the views module registers every view on its model
# (register_model_view); get_model_urls() below reads that registry.
from . import views  # noqa: F401
from utilities.urls import get_model_urls

urlpatterns = (
    path('inventree-instances/', include(get_model_urls('netbox_inventree_sync', 'inventreeinstance', detail=False))),
    path('inventree-instances/<int:pk>/', include(get_model_urls('netbox_inventree_sync', 'inventreeinstance'))),

    path('sync-mappings/', include(get_model_urls('netbox_inventree_sync', 'syncmapping', detail=False))),
    path('sync-mappings/<int:pk>/', include(get_model_urls('netbox_inventree_sync', 'syncmapping'))),

    # Read-only status screens; neither model has detail/edit views, so
    # only the (detail=False) list routes exist for them.
    path('synced-objects/', include(get_model_urls('netbox_inventree_sync', 'syncedobject', detail=False))),
    path('sync-log/', include(get_model_urls('netbox_inventree_sync', 'synclogentry', detail=False))),
)
