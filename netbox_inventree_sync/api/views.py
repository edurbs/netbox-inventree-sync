"""API views for the InvenTree sync plugin.

Model viewsets follow NetBox's plugin conventions
(``NetBoxModelViewSet`` with default ``TokenPermissions``: authenticated
users plus per-model Django permissions). Two bespoke endpoints:

* ``GET /fields/`` — the mapping-builder matrix: every mappable NetBox
  field of a content type, every writable InvenTree field of an endpoint
  (each annotated with the NetBox fields the compatibility matrix
  accepts), and the content types available as pointer pairings.
* ``POST /mappings/<id>/sync/`` — "Sync now": enqueue a one-shot
  ``ReconcileJob`` for a mapping (in addition to any recurring
  schedule), mirroring NetBox's own DataSource sync action.
"""

from dataclasses import asdict

from django.contrib.contenttypes.models import ContentType
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.reverse import reverse
from rest_framework.views import APIView

from netbox.api.viewsets import NetBoxModelViewSet, NetBoxReadOnlyModelViewSet

from netbox_inventree_sync.filtersets import (
    InvenTreeInstanceFilterSet,
    SyncLogEntryFilterSet,
    SyncMappingFilterSet,
    SyncedObjectFilterSet,
)
from netbox_inventree_sync.jobs import ReconcileJob
from netbox_inventree_sync.models import InvenTreeInstance, SyncLogEntry, SyncMapping, SyncedObject
from netbox_inventree_sync.sync.client import InvenTreeClient, InvenTreeClientError
from netbox_inventree_sync.sync.compatibility import compatible
from netbox_inventree_sync.sync.fields import get_netbox_fields

from . import serializers

__all__ = (
    'InvenTreeInstanceViewSet',
    'InvenTreeUnavailable',
    'MappableFieldsView',
    'SyncLogEntryViewSet',
    'SyncMappingViewSet',
    'SyncedObjectViewSet',
)


class InvenTreeUnavailable(APIException):
    """The InvenTree server could not be queried for endpoint metadata."""

    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = 'The InvenTree server could not be reached.'
    default_code = 'inventree_unavailable'


class InvenTreeInstanceViewSet(NetBoxModelViewSet):
    queryset = InvenTreeInstance.objects.all()
    serializer_class = serializers.InvenTreeInstanceSerializer
    filterset_class = InvenTreeInstanceFilterSet


class SyncMappingViewSet(NetBoxModelViewSet):
    queryset = SyncMapping.objects.all()
    serializer_class = serializers.SyncMappingSerializer
    filterset_class = SyncMappingFilterSet

    @extend_schema(request=None)
    @action(detail=True, methods=['post'])
    def sync(self, request, pk):
        """Enqueue a one-shot reconcile job for this mapping ("Sync now").

        Unlike the recurring schedule maintained by the model, this job
        carries no interval and runs at the first available opportunity.
        """
        mapping = get_object_or_404(self.get_queryset(), pk=pk)
        job = ReconcileJob.enqueue(instance=mapping, mapping_id=mapping.pk, user=request.user)
        return Response({
            'job': {
                'id': job.pk,
                'url': reverse('core-api:job-detail', kwargs={'pk': job.pk}, request=request),
                'status': job.status,
            },
        }, status=status.HTTP_202_ACCEPTED)


class SyncedObjectViewSet(NetBoxReadOnlyModelViewSet):
    queryset = SyncedObject.objects.all()
    serializer_class = serializers.SyncedObjectSerializer
    filterset_class = SyncedObjectFilterSet


class SyncLogEntryViewSet(NetBoxReadOnlyModelViewSet):
    queryset = SyncLogEntry.objects.all()
    serializer_class = serializers.SyncLogEntrySerializer
    filterset_class = SyncLogEntryFilterSet


class MappableFieldsView(APIView):
    """Compatibility matrix between one NetBox content type and one InvenTree endpoint.

    ``GET fields/?connection=<id>&content_type=<ct_id>&endpoint=<path>``
    returns the NetBox field specs, the InvenTree field specs (each with
    the ``compatible_netbox_fields`` names the Task 6 matrix accepts for
    it), and the connection's available pairing keys (the
    ``'<app>.<model>'`` content types of its enabled mappings).

    NetBox's default ``TokenPermissions`` derives permission codes from a
    viewset's queryset/model; this view deliberately spans two plugin
    models, so it declares plain authentication-based permissions.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        parameters=[serializers.MappableFieldsQuerySerializer],
        responses={200: serializers.MappableFieldsResponseSerializer},
    )
    def get(self, request):
        connection = self._get_connection(request)
        content_type = self._get_content_type(request)
        endpoint = (request.query_params.get('endpoint') or '').strip()
        if not endpoint:
            raise ValidationError({'endpoint': 'This query parameter is required (e.g. /api/part/).'})

        netbox_specs = get_netbox_fields(content_type)
        available_pairings = self._available_pairings(connection)
        try:
            client = InvenTreeClient(connection.url, connection.api_token)
            inventree_specs = client.get_field_specs(endpoint)
        except InvenTreeClientError as exc:
            # 502-style: the failure is upstream, not in the request.
            raise InvenTreeUnavailable(str(exc))

        return Response({
            'netbox': [asdict(spec) for spec in netbox_specs],
            'inventree': [
                {
                    **asdict(spec),
                    'compatible_netbox_fields': [
                        netbox_spec.name for netbox_spec in netbox_specs
                        if compatible(netbox_spec, spec, available_pairings)
                    ],
                }
                for spec in inventree_specs
            ],
            'available_pairings': sorted(available_pairings),
        })

    @staticmethod
    def _get_connection(request):
        raw = request.query_params.get('connection')
        if raw is None:
            raise ValidationError({'connection': 'This query parameter is required.'})
        # Restrict to connections the user may view, mirroring the viewsets'
        # object-level restriction; a forbidden connection is then
        # indistinguishable from a nonexistent one.
        queryset = InvenTreeInstance.objects.restrict(request.user, 'view')
        try:
            connection = queryset.get(pk=raw)
        except (InvenTreeInstance.DoesNotExist, ValueError, TypeError):
            raise ValidationError({'connection': f'No InvenTree instance with ID {raw!r} exists.'})
        if not connection.enabled:
            raise ValidationError({'connection': f'InvenTree instance {connection.name!r} is disabled.'})
        return connection

    @staticmethod
    def _get_content_type(request):
        raw = request.query_params.get('content_type')
        if raw is None:
            raise ValidationError({'content_type': 'This query parameter is required.'})
        try:
            return ContentType.objects.get_for_id(raw)
        except (ContentType.DoesNotExist, ValueError, TypeError):
            raise ValidationError({'content_type': f'No content type with ID {raw!r} exists.'})

    @staticmethod
    def _available_pairings(connection) -> set:
        """``'<app>.<model>'`` keys of the connection's enabled mappings."""
        return set(
            f'{app_label}.{model}'
            for app_label, model in SyncMapping.objects.filter(
                connection=connection,
                enabled=True,
            ).values_list('netbox_content_type__app_label', 'netbox_content_type__model')
        )
