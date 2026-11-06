"""FilterSets for the plugin's list views."""

import django_filters
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from netbox.filtersets import NetBoxModelFilterSet

from .models import InvenTreeInstance, SyncMapping

__all__ = (
    'InvenTreeInstanceFilterSet',
    'SyncMappingFilterSet',
)


class InvenTreeInstanceFilterSet(NetBoxModelFilterSet):

    class Meta:
        model = InvenTreeInstance
        fields = ('id', 'name', 'url', 'enabled')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(name__icontains=value) |
            Q(url__icontains=value)
        )


class SyncMappingFilterSet(NetBoxModelFilterSet):
    connection_id = django_filters.ModelMultipleChoiceFilter(
        field_name='connection',
        queryset=InvenTreeInstance.objects.all(),
        label=_('Connection (ID)'),
    )

    class Meta:
        model = SyncMapping
        fields = ('id', 'name', 'inventree_endpoint', 'enabled', 'reconcile_interval_minutes')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(name__icontains=value) |
            Q(description__icontains=value) |
            Q(inventree_endpoint__icontains=value)
        )
