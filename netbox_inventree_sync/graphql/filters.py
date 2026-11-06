"""GraphQL filter inputs for the plugin's models (tutorial step 11)."""

import strawberry_django
from strawberry import ID
from strawberry_django import StrFilterLookup

from netbox.graphql.filters import NetBoxModelFilter
from netbox_inventree_sync import models

__all__ = (
    'InvenTreeInstanceFilter',
    'SyncMappingFilter',
)


@strawberry_django.filter_type(models.InvenTreeInstance, lookups=True)
class InvenTreeInstanceFilter(NetBoxModelFilter):

    name: StrFilterLookup | None = strawberry_django.filter_field()
    url: StrFilterLookup | None = strawberry_django.filter_field()
    enabled: bool | None = strawberry_django.filter_field()


@strawberry_django.filter_type(models.SyncMapping, lookups=True)
class SyncMappingFilter(NetBoxModelFilter):

    name: StrFilterLookup | None = strawberry_django.filter_field()
    description: StrFilterLookup | None = strawberry_django.filter_field()
    inventree_endpoint: StrFilterLookup | None = strawberry_django.filter_field()
    connection_id: ID | None = strawberry_django.filter_field()
    enabled: bool | None = strawberry_django.filter_field()
