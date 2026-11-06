"""NetBox plugin: synchronize NetBox inventory data to InvenTree."""

from netbox.plugins import PluginConfig

__version__ = '0.1.0'


class NetBoxInvenTreeSyncConfig(PluginConfig):
    name = 'netbox_inventree_sync'
    verbose_name = 'InvenTree Sync'
    description = 'Synchronize NetBox inventory data to InvenTree'
    version = __version__
    # Plugin navigation/menu items are introduced in a later task.
    # menu = ...


config = NetBoxInvenTreeSyncConfig
