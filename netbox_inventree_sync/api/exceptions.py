"""API-layer exceptions shared by the plugin's serializers and views.

Kept in its own module so both the view layer (fields endpoint) and the
serializer layer (mapping writes) translate an unreachable InvenTree
into the same 502 semantics without importing each other.
"""

from rest_framework import status
from rest_framework.exceptions import APIException

__all__ = ('InvenTreeUnavailable',)


class InvenTreeUnavailable(APIException):
    """The InvenTree server could not be queried for endpoint metadata."""

    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = 'The InvenTree server could not be reached.'
    default_code = 'inventree_unavailable'
