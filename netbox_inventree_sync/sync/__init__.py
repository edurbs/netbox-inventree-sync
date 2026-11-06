"""InvenTree HTTP transport (pure, no NetBox imports).

Sync logic and NetBox models live elsewhere; this package currently holds
only the REST client used to talk to an InvenTree server.
"""

from netbox_inventree_sync.sync.client import (
    InvenTreeClient,
    InvenTreeClientError,
    InventreeFieldSpec,
)

__all__ = ['InvenTreeClient', 'InvenTreeClientError', 'InventreeFieldSpec']
