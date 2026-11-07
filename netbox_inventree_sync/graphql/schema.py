"""Query roots exposed on NetBox's GraphQL API (tutorial step 11)."""

import strawberry
import strawberry_django

from .types import InvenTreeInstanceType, SyncMappingType

__all__ = ('NetBoxInvenTreeSyncQuery',)


@strawberry.type(name='Query')
class NetBoxInvenTreeSyncQuery:
    """Plugin query fields merged into NetBox's root ``Query`` type."""

    inventreeinstance: InvenTreeInstanceType = strawberry_django.field()
    inventreeinstance_list: list[InvenTreeInstanceType] = strawberry_django.field()

    syncmapping: SyncMappingType = strawberry_django.field()
    syncmapping_list: list[SyncMappingType] = strawberry_django.field()
