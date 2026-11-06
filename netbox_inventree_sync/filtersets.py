"""FilterSets for the plugin's list views."""

import django_filters
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from netbox.filtersets import NetBoxModelFilterSet

from .choices import SyncActionChoices, SyncStatusChoices
from .models import InvenTreeInstance, SyncLogEntry, SyncMapping, SyncedObject

__all__ = (
    'InvenTreeInstanceFilterSet',
    'SyncLogEntryFilterSet',
    'SyncMappingFilterSet',
    'SyncedObjectFilterSet',
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


class SyncedObjectFilterSet(NetBoxModelFilterSet):

    status = django_filters.MultipleChoiceFilter(
        choices=SyncStatusChoices,
        distinct=False,
    )
    mapping_id = django_filters.ModelMultipleChoiceFilter(
        field_name='mapping',
        queryset=SyncMapping.objects.all(),
        label=_('Mapping (ID)'),
    )

    class Meta:
        model = SyncedObject
        fields = ('id', 'status', 'mapping_id')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(mapping__name__icontains=value) |
            Q(mapping__inventree_endpoint__icontains=value) |
            Q(last_error__icontains=value)
        )


class SyncLogEntryFilterSet(NetBoxModelFilterSet):

    action = django_filters.MultipleChoiceFilter(
        choices=SyncActionChoices,
        distinct=False,
    )
    synced_id = django_filters.ModelMultipleChoiceFilter(
        field_name='synced',
        queryset=SyncedObject.objects.all(),
        label=_('Synced object (ID)'),
    )

    class Meta:
        model = SyncLogEntry
        fields = ('id', 'action', 'synced_id')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(synced__mapping__name__icontains=value) |
            Q(response_body__icontains=value)
        )
