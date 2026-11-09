"""NetBox plugin: synchronize NetBox inventory data to InvenTree."""

import logging
from importlib import metadata

from netbox.plugins import PluginConfig

try:
    # Single source of truth: the version lives in pyproject.toml; read it
    # from the installed package metadata (editable installs ship metadata
    # too, so the dev/test venvs resolve it as well).
    __version__ = metadata.version('netbox-inventree-sync')
except metadata.PackageNotFoundError:
    # Importing from a source tree that was never installed (no pip
    # install, editable or otherwise) — keep the plugin importable.
    __version__ = '0.0.0'

logger = logging.getLogger('netbox_inventree_sync')


class NetBoxInvenTreeSyncConfig(PluginConfig):
    name = 'netbox_inventree_sync'
    verbose_name = 'InvenTree Sync'
    description = 'Synchronize NetBox inventory data to InvenTree'
    version = __version__
    min_version = '4.7'
    menu = 'navigation.menu'

    def ready(self):
        # Connect the sync signal wiring. This only connects signal hooks
        # (and may run one guarded binding build when apps are already
        # populated — during a plain web/worker boot they are, so a
        # single exception-guarded query happens here). At first boot (or
        # while `manage.py test` builds the test database) the plugin's
        # tables may not even exist, hence the guard. Whatever bindings
        # are missed here converge later, on post_migrate, on the first
        # request in the process (TTL re-check), or on the next mapping
        # save — see signals.register_receivers(). Anything unexpected at
        # boot must not take NetBox down.
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
