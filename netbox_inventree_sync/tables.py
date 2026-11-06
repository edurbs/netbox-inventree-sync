"""Tables for the plugin's models."""

import django_tables2 as tables
from django.utils.translation import gettext_lazy as _

from netbox.tables import NetBoxTable, columns

from .models import InvenTreeInstance, SyncFieldPair, SyncMapping

__all__ = (
    'InvenTreeInstanceTable',
    'SyncFieldPairTable',
    'SyncMappingTable',
)


class InvenTreeInstanceTable(NetBoxTable):
    name = tables.Column(
        verbose_name=_('Name'),
        linkify=True,
    )
    url = tables.URLColumn(
        verbose_name=_('URL'),
    )
    enabled = columns.BooleanColumn(
        verbose_name=_('Enabled'),
    )

    class Meta(NetBoxTable.Meta):
        model = InvenTreeInstance
        fields = ('pk', 'id', 'name', 'url', 'enabled', 'actions')
        default_columns = ('pk', 'name', 'url', 'enabled')


class SyncMappingTable(NetBoxTable):
    name = tables.Column(
        verbose_name=_('Name'),
        linkify=True,
    )
    connection = tables.Column(
        verbose_name=_('Connection'),
        linkify=True,
    )
    netbox_content_type = tables.Column(
        verbose_name=_('NetBox type'),
        order_by=('netbox_content_type__app_label', 'netbox_content_type__model'),
    )
    inventree_endpoint = tables.Column(
        verbose_name=_('InvenTree endpoint'),
    )
    reconcile_interval_minutes = tables.Column(
        verbose_name=_('Reconcile (minutes)'),
    )
    enabled = columns.BooleanColumn(
        verbose_name=_('Enabled'),
    )
    description = tables.Column(
        verbose_name=_('Description'),
    )

    class Meta(NetBoxTable.Meta):
        model = SyncMapping
        fields = (
            'pk', 'id', 'name', 'connection', 'netbox_content_type', 'inventree_endpoint',
            'reconcile_interval_minutes', 'enabled', 'description', 'actions',
        )
        default_columns = ('pk', 'name', 'connection', 'netbox_content_type', 'inventree_endpoint', 'enabled')

    def render_netbox_content_type(self, record):
        content_type = record.netbox_content_type
        return f'{content_type.app_label}.{content_type.model}'


class SyncFieldPairTable(NetBoxTable):
    """Read-only listing of a mapping's field pairs (builder screen in a later task)."""

    # The base NetBoxTable linkifies `id` via get_absolute_url(); the
    # pairs have no standalone views until the builder screen arrives,
    # so linking them would break URL resolution.
    id = tables.Column(
        verbose_name=_('ID'),
    )
    netbox_field = tables.Column(
        verbose_name=_('NetBox field'),
    )
    netbox_subfield = tables.Column(
        verbose_name=_('Subfield'),
    )
    inventree_field = tables.Column(
        verbose_name=_('InvenTree field'),
    )
    transform = columns.ChoiceFieldColumn(
        verbose_name=_('Transform'),
    )

    class Meta(NetBoxTable.Meta):
        model = SyncFieldPair
        fields = ('pk', 'id', 'netbox_field', 'netbox_subfield', 'inventree_field', 'transform')
        default_columns = ('netbox_field', 'netbox_subfield', 'inventree_field', 'transform')

    # The pairs have no standalone CRUD views (they are managed on the
    # mapping builder screen), so no row actions are offered.
    actions = columns.ActionsColumn(
        actions=(),
    )
