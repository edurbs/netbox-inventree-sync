"""Strawberry types for the plugin's models (tutorial step 11).

``InvenTreeInstance.api_token`` is deliberately absent from the GraphQL
surface: the field list of ``InvenTreeInstanceType`` enumerates every exposed
model field explicitly, and the token is not among them.
"""

from typing import TYPE_CHECKING, Annotated

import strawberry
import strawberry_django
from netbox.graphql.types import NetBoxObjectType
from netbox_inventree_sync import models

from .filters import InvenTreeInstanceFilter, SyncMappingFilter

if TYPE_CHECKING:
    from netbox.graphql.types import ContentTypeType

__all__ = (
    'InvenTreeInstanceType',
    'SyncMappingType',
)


@strawberry_django.type(
    models.InvenTreeInstance,
    # Explicit field list: api_token must never reach the GraphQL API.
    fields=['id', 'name', 'url', 'enabled'],
    filters=InvenTreeInstanceFilter,
)
class InvenTreeInstanceType(NetBoxObjectType):
    """A remote InvenTree server NetBox pushes inventory data to."""


@strawberry_django.type(
    models.SyncMapping,
    # FK relations (connection, netbox_content_type) are declared as
    # annotations below rather than through the fields list.
    fields=[
        'id', 'name', 'inventree_endpoint',
        'match_netbox_field', 'match_inventree_field',
        'reconcile_interval_minutes', 'enabled', 'description',
    ],
    filters=SyncMappingFilter,
)
class SyncMappingType(NetBoxObjectType):
    """A mapping from one NetBox content type to one InvenTree endpoint."""

    connection: InvenTreeInstanceType
    netbox_content_type: Annotated['ContentTypeType', strawberry.lazy('netbox.graphql.types')]
