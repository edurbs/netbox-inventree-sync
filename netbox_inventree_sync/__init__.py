"""NetBox plugin: synchronize NetBox inventory data to InvenTree."""

import logging

from netbox.plugins import PluginConfig

__version__ = '0.1.0'

logger = logging.getLogger('netbox_inventree_sync')


class NetBoxInvenTreeSyncConfig(PluginConfig):
    name = 'netbox_inventree_sync'
    verbose_name = 'InvenTree Sync'
    description = 'Synchronize NetBox inventory data to InvenTree'
    version = __version__
    # Plugin navigation/menu items are introduced in a later task.
    # menu = ...

    def ready(self):
        # Connect the sync signal wiring. This only connects signal hooks —
        # no database access: Django forbids queries during app
        # initialization, and at first boot (or while `manage.py test`
        # builds the test database) the plugin's tables may not even exist.
        # The actual receiver bindings (the content types of the enabled
        # mappings) are built later, on post_migrate, on the first request
        # in the process, or on the next mapping save — see
        # signals.register_receivers(). The try/except stays as a guard:
        # anything unexpected at boot must not take NetBox down.
        super().ready()
        try:
            from netbox_inventree_sync import signals
            signals.register_receivers()
        except Exception:
            logger.exception(
                'netbox_inventree_sync: signal receiver hooks not connected; '
                'they will be connected on the next migrate or first request'
            )


config = NetBoxInvenTreeSyncConfig
