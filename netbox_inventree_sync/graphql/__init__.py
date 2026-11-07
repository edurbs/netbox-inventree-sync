"""GraphQL schema resources for the InvenTree sync plugin.

NetBox 4.7 loads plugin GraphQL schemas lazily: when the root schema is
assembled (``netbox.graphql.schema``), it imports every installed plugin's
``graphql.schema`` resource — the package's ``schema`` attribute — and splices
the listed query classes into the root ``Query`` type (see
``PluginConfig._load_resource`` / ``_load_plugin_graphql_schemas``). Nothing
here is imported during ``django.setup()``, so management commands that never
touch GraphQL skip the cost.
"""

from .schema import NetBoxInvenTreeSyncQuery

__all__ = ('NetBoxInvenTreeSyncQuery',)

schema = [
    NetBoxInvenTreeSyncQuery,
]
