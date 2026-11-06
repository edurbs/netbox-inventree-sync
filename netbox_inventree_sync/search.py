"""Global-search indexes for the plugin's primary objects (tutorial step 12).

``PluginConfig.ready()`` imports this module through its default
``search.indexes`` resource path, so the ``@register_search`` decorators are
all the wiring needed — no explicit ``search_indexes`` attribute on the
PluginConfig (verified against netbox/plugins/__init__.py and
docs/plugins/development/search.md).
"""

from netbox.search import SearchIndex, register_search
from netbox_inventree_sync.models import InvenTreeInstance, SyncMapping

__all__ = (
    'InvenTreeInstanceIndex',
    'SyncMappingIndex',
)


@register_search
class InvenTreeInstanceIndex(SearchIndex):
    model = InvenTreeInstance
    fields = (
        ('name', 100),
        ('url', 1000),
    )
    display_attrs = ('name',)


@register_search
class SyncMappingIndex(SearchIndex):
    model = SyncMapping
    fields = (
        ('name', 100),
        ('description', 500),
        ('inventree_endpoint', 1000),
    )
    display_attrs = ('name', 'inventree_endpoint')
