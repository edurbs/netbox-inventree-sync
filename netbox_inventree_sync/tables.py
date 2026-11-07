"""Tables for the plugin's models."""

import json

import django_tables2 as tables
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from netbox.tables import NetBoxTable, columns

from .choices import SyncActionChoices, SyncStatusChoices
from .models import InvenTreeInstance, SyncFieldPair, SyncLogEntry, SyncMapping, SyncedObject

__all__ = (
    'InvenTreeInstanceTable',
    'SyncFieldPairTable',
    'SyncLogEntryTable',
    'SyncMappingTable',
    'SyncedObjectTable',
)

# Bootstrap badge classes per link status / log action.
STATUS_BADGE_COLORS = {
    SyncStatusChoices.PENDING: 'info',
    SyncStatusChoices.SYNCED: 'success',
    SyncStatusChoices.FAILED: 'danger',
    SyncStatusChoices.ORPHANED: 'warning',
}

ACTION_BADGE_COLORS = {
    SyncActionChoices.CREATE: 'success',
    SyncActionChoices.UPDATE: 'info',
    SyncActionChoices.DEACTIVATE: 'warning',
    SyncActionChoices.ADOPT: 'secondary',
    SyncActionChoices.ERROR: 'danger',
}


def _inventree_object_url(connection, endpoint, pk):
    """Build the human-facing InvenTree URL of one remote object.

    ``<connection.url>`` + the endpoint's UI path + pk: endpoint
    ``/api/part/`` and pk 42 become ``<url>/part/42/`` (the REST ``/api``
    prefix is stripped). A missing pk (never created remotely) yields
    ``''`` so callers can render a placeholder instead of a link.
    """
    if not pk:
        return ''
    path = endpoint or ''
    if path.startswith('/api'):
        path = path[4:] or '/'
    if not path.startswith('/'):
        path = f'/{path}'
    if not path.endswith('/'):
        path = f'{path}/'
    return f'{(connection.url or "").rstrip("/")}{path}{pk}/'


def _inventree_object_label(endpoint, pk):
    """Human label for a remote object, e.g. endpoint '/api/part/' → 'Part #42'."""
    resource = (endpoint or '').rstrip('/').rsplit('/', 1)[-1].replace('_', ' ') or 'object'
    return f'{resource.title()} #{pk}' if pk else resource.title()


def _truncate(value, length=100):
    """Collapse long error/response texts for table cells (full text
    stays available in the source data / log detail)."""
    if not value:
        return '—'
    if len(value) <= length:
        return value
    return f'{value[:length]}…'


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


class SyncedObjectTable(NetBoxTable):
    """Read-only status of every tracked NetBox → InvenTree link row."""

    # No detail view is registered for the model, so the id column must
    # not linkify (NetBoxTable's default would reverse a nonexistent URL).
    id = tables.Column(
        verbose_name=_('ID'),
    )
    mapping = tables.Column(
        verbose_name=_('Mapping'),
        linkify=True,
    )
    # The custom columns carry an ``accessor`` resolving to a real model
    # field: django-tables2 skips render_FOO entirely when a column's
    # accessor fails to resolve on the record (it returns the column
    # default instead), so a bare accessor-less column would never call
    # the render methods below.
    netbox_object = tables.Column(
        verbose_name=_('NetBox object'),
        accessor='netbox_object_id',
        order_by=('mapping__netbox_content_type', 'netbox_object_id'),
    )
    status = columns.ChoiceFieldColumn(
        verbose_name=_('Status'),
        color=lambda record: STATUS_BADGE_COLORS.get(record.status, 'secondary'),
    )
    inventree_object = tables.Column(
        verbose_name=_('InvenTree object'),
        accessor='inventree_pk',
        order_by='inventree_pk',
    )
    last_synced_at = columns.DateTimeColumn(
        verbose_name=_('Last synced'),
    )
    last_error = tables.Column(
        verbose_name=_('Last error'),
    )

    class Meta(NetBoxTable.Meta):
        model = SyncedObject
        fields = (
            'pk', 'id', 'mapping', 'netbox_object', 'status', 'inventree_object',
            'last_synced_at', 'last_error', 'actions',
        )
        default_columns = (
            'id', 'mapping', 'netbox_object', 'status', 'inventree_object', 'last_synced_at',
            'last_error',
        )

    # Read-only listing: no per-row edit/delete/changelog actions (those
    # views are not registered for the model).
    actions = columns.ActionsColumn(
        actions=(),
    )

    def render_netbox_object(self, record):
        """Link the mapped NetBox object; plain text once it is deleted.

        The per-row object lookup is a primary-key query, bounded by the
        page size; the mapping (and its content type) come preloaded via
        the view queryset's select_related().
        """
        content_type = record.mapping.netbox_content_type
        model = content_type.model_class()
        if model is not None:
            obj = model.objects.filter(pk=record.netbox_object_id).first()
            if obj is not None:
                return format_html('<a href="{}">{}</a>', obj.get_absolute_url(), obj)
        return format_html('{} #{}', f'{content_type.app_label}.{content_type.model}', record.netbox_object_id)

    def value_netbox_object(self, record):
        content_type = record.mapping.netbox_content_type
        return f'{content_type.app_label}.{content_type.model} #{record.netbox_object_id}'

    def render_inventree_object(self, record):
        url = _inventree_object_url(
            record.mapping.connection, record.mapping.inventree_endpoint, record.inventree_pk,
        )
        if not url:
            return '—'
        label = _inventree_object_label(record.mapping.inventree_endpoint, record.inventree_pk)
        return format_html('<a href="{}" target="_blank" rel="noopener noreferrer">{}</a>', url, label)

    def value_inventree_object(self, record):
        return record.inventree_pk

    def render_last_error(self, value):
        return _truncate(value)


class SyncLogEntryTable(NetBoxTable):
    """Read-only audit of every push attempt against InvenTree."""

    id = tables.Column(
        verbose_name=_('ID'),
    )
    synced = tables.Column(
        verbose_name=_('Synced object'),
    )
    action = columns.ChoiceFieldColumn(
        verbose_name=_('Action'),
        color=lambda record: ACTION_BADGE_COLORS.get(record.action, 'secondary'),
    )
    payload = tables.Column(
        verbose_name=_('Payload'),
        orderable=False,
    )
    response_status = tables.Column(
        verbose_name=_('Response status'),
    )
    response_body = tables.Column(
        verbose_name=_('Response body'),
        orderable=False,
    )
    created = columns.DateTimeColumn(
        verbose_name=_('Created'),
    )

    class Meta(NetBoxTable.Meta):
        model = SyncLogEntry
        fields = (
            'pk', 'id', 'synced', 'action', 'payload', 'response_status', 'response_body',
            'created', 'actions',
        )
        default_columns = (
            'id', 'synced', 'action', 'payload', 'response_status', 'response_body', 'created',
        )

    # Read-only listing (see SyncedObjectTable.actions).
    actions = columns.ActionsColumn(
        actions=(),
    )

    def render_payload(self, record):
        """The recorded payload as a collapsed <details> block, keeping
        rows scannable while the full JSON stays one click away."""
        if not record.payload:
            return '—'
        formatted = json.dumps(record.payload, indent=2, sort_keys=True, default=str)
        # format_html escapes its arguments, so the JSON text must go in
        # unescaped (pre-escaping it would double-escape to &amp;quot;).
        return format_html(
            '<details><summary class="text-muted">…</summary><pre class="mb-0">{}</pre></details>',
            formatted,
        )

    def value_payload(self, record):
        return json.dumps(record.payload, default=str) if record.payload else ''

    def render_response_status(self, value):
        if value is None:
            return '—'
        color = 'success' if value < 400 else 'danger'
        return format_html('<span class="badge text-bg-{}">{}</span>', color, value)

    def render_response_body(self, value):
        return _truncate(value)
