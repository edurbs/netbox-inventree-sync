"""UI views: tutorial-style CRUD for connections and sync mappings.

Every view is registered with ``register_model_view()`` so the plugin's
``urls.py`` can wire them via ``get_model_urls()`` — the same mechanism
that automatically mounts NetBox's feature views (changelog, jobs) for
the models. The mapping builder screen (:class:`SyncMappingBuilderView`)
is mounted the same way, as a ``builder/`` sub-path of each mapping.
"""

import json
from dataclasses import asdict

from django.contrib import messages
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls.exceptions import NoReverseMatch
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from django.views import View
from netbox.object_actions import AddObject, BulkDelete, BulkExport
from netbox.views import generic
from utilities.views import ObjectPermissionRequiredMixin, register_model_view

from . import filtersets, forms, tables
from .jobs import ReconcileJob
from .models import InvenTreeInstance, SyncFieldPair, SyncLogEntry, SyncMapping, SyncedObject
from .sync.client import InvenTreeClient, InvenTreeClientError
from .sync.compatibility import compatible
from .sync.fields import get_netbox_fields
from .sync.validation import (
    _available_pairings,
    validate_mapping,
    validate_mapping_graph_for_write,
)

__all__ = (
    'InvenTreeInstanceListView',
    'InvenTreeInstanceView',
    'InvenTreeInstanceEditView',
    'InvenTreeInstanceDeleteView',
    'InvenTreeInstanceBulkDeleteView',
    'SyncMappingListView',
    'SyncMappingView',
    'SyncMappingEditView',
    'SyncMappingDeleteView',
    'SyncMappingBulkDeleteView',
    'SyncMappingBuilderView',
    'SyncMappingSyncView',
    'SyncLogEntryListView',
    'SyncedObjectListView',
)


#
# InvenTree instances (connections)
#


@register_model_view(InvenTreeInstance, 'list', path='', detail=False)
class InvenTreeInstanceListView(generic.ObjectListView):
    queryset = InvenTreeInstance.objects.all()
    filterset = filtersets.InvenTreeInstanceFilterSet
    filterset_form = forms.InvenTreeInstanceFilterForm
    table = tables.InvenTreeInstanceTable
    template_name = 'netbox_inventree_sync/inventreeinstance_list.html'
    actions = (AddObject, BulkExport, BulkDelete)


@register_model_view(InvenTreeInstance)
class InvenTreeInstanceView(generic.ObjectView):
    queryset = InvenTreeInstance.objects.all()

    def get_extra_context(self, request, instance):
        mappings_table = tables.SyncMappingTable(
            SyncMapping.objects.filter(connection=instance),
        )
        return {
            'mappings_table': mappings_table,
        }


@register_model_view(InvenTreeInstance, 'add', detail=False)
@register_model_view(InvenTreeInstance, 'edit')
class InvenTreeInstanceEditView(generic.ObjectEditView):
    queryset = InvenTreeInstance.objects.all()
    form = forms.InvenTreeInstanceForm
    template_name = 'netbox_inventree_sync/inventreeinstance_edit.html'

    def post(self, request, *args, **kwargs):
        if request.POST.get('test_connection'):
            return self.test_connection(request, *args, **kwargs)
        return super().post(request, *args, **kwargs)

    def test_connection(self, request, *args, **kwargs):
        """Handle the ``Test connection`` submit of the edit form.

        Probes the InvenTree server with the values currently in the form
        (a blank token falls back to the stored one, since the form never
        echoes it back) and reports the outcome via Django messages. This
        path deliberately saves nothing: the form is re-rendered as-is so
        the user can fix it and submit normally.
        """
        obj = self.get_object(**kwargs)
        url = request.POST.get('url') or (obj.url if obj.pk else '')
        token = request.POST.get('api_token') or (obj.api_token if obj.pk else '')
        try:
            info = InvenTreeClient(url, token).test_connection()
            messages.success(request, _('Connection to %(url)s successful: authenticated as %(user)s '
                                        '(InvenTree %(version)s)') % {
                'url': url,
                'user': info.get('user'),
                'version': info.get('version'),
            })
        except InvenTreeClientError as exc:
            messages.error(request, _('Connection to %(url)s failed: %(error)s') % {
                'url': url,
                'error': exc,
            })
        form = self.form(data=request.POST, instance=obj)
        return render(request, self.template_name, {
            'model': self.queryset.model,
            'object': obj,
            'form': form,
            'return_url': self.get_return_url(request, obj),
            **self.get_extra_context(request, obj),
        })


@register_model_view(InvenTreeInstance, 'delete')
class InvenTreeInstanceDeleteView(generic.ObjectDeleteView):
    queryset = InvenTreeInstance.objects.all()


@register_model_view(InvenTreeInstance, 'bulk_delete', path='delete', detail=False)
class InvenTreeInstanceBulkDeleteView(generic.BulkDeleteView):
    queryset = InvenTreeInstance.objects.all()
    filterset = filtersets.InvenTreeInstanceFilterSet
    table = tables.InvenTreeInstanceTable


#
# Sync mappings
#


def _report_protected_objects(request, objs, dependent_objects):
    """Report a blocked deletion without linking the dependent objects.

    NetBox's default handler links every protected object through its
    ``get_absolute_url()``; field pairs and synced objects carry such a
    method (model layer) but no routes until the builder/status screens
    arrive, so linking them would fail URL resolution. Plain names keep
    the delete views functional. Callers perform their own redirect.
    """
    objs = list(objs)
    dependent_objects = list(dependent_objects)
    messages.error(
        request,
        mark_safe(
            _('Unable to delete <strong>{objects}</strong>. {count} dependent objects were found: {dependents}').format(
                objects=', '.join(escape(str(obj)) for obj in objs),
                count=len(dependent_objects),
                dependents=', '.join(escape(str(dependent)) for dependent in dependent_objects[:50]),
            )
        ),
    )


@register_model_view(SyncMapping, 'list', path='', detail=False)
class SyncMappingListView(generic.ObjectListView):
    queryset = SyncMapping.objects.all()
    filterset = filtersets.SyncMappingFilterSet
    filterset_form = forms.SyncMappingFilterForm
    table = tables.SyncMappingTable
    template_name = 'netbox_inventree_sync/syncmapping_list.html'
    actions = (AddObject, BulkExport, BulkDelete)


@register_model_view(SyncMapping)
class SyncMappingView(generic.ObjectView):
    queryset = SyncMapping.objects.all()

    def get_extra_context(self, request, instance):
        # Field pairs are managed on the builder screen (later task);
        # the detail view lists them read-only.
        field_pairs_table = tables.SyncFieldPairTable(instance.field_pairs.all())
        return {
            'field_pairs_table': field_pairs_table,
        }


@register_model_view(SyncMapping, 'add', detail=False)
@register_model_view(SyncMapping, 'edit')
class SyncMappingEditView(generic.ObjectEditView):
    queryset = SyncMapping.objects.all()
    form = forms.SyncMappingForm
    template_name = 'netbox_inventree_sync/syncmapping_edit.html'


@register_model_view(SyncMapping, 'delete')
class SyncMappingDeleteView(generic.ObjectDeleteView):
    queryset = SyncMapping.objects.all()

    def _handle_protected_objects(self, obj, protected_objects, request, exc):
        # GET/confirm path: NetBox links protected objects here (see
        # _report_protected_objects for why that must not happen yet).
        _report_protected_objects(request, [obj], protected_objects)
        return redirect(obj.get_absolute_url())

    def post(self, request, *args, **kwargs):
        try:
            return super().post(request, *args, **kwargs)
        except NoReverseMatch:
            # Deleting a protected mapping is blocked by the database and
            # caught by NetBox's handler, which then tries to link each
            # dependent object — and fails URL resolution for the pairs /
            # synced objects, which have no routes yet. Nothing has been
            # deleted at that point; report the dependents without links.
            obj = self.get_object(**kwargs)
            _report_protected_objects(
                request, [obj], list(obj.field_pairs.all()) + list(obj.synced_objects.all()),
            )
            return redirect(obj.get_absolute_url())


@register_model_view(SyncMapping, 'bulk_delete', path='delete', detail=False)
class SyncMappingBulkDeleteView(generic.BulkDeleteView):
    queryset = SyncMapping.objects.all()
    filterset = filtersets.SyncMappingFilterSet
    table = tables.SyncMappingTable

    def post(self, request, **kwargs):
        try:
            return super().post(request, **kwargs)
        except NoReverseMatch:
            # Same protection as SyncMappingDeleteView.post(): a protected
            # mapping aborts the whole (atomic) bulk deletion and NetBox's
            # handler then fails URL resolution while linking the pairs /
            # synced objects, which have no routes yet. Nothing has been
            # deleted; report the dependents without links.
            if request.POST.get('_all'):
                qs = self.queryset.model.objects.all()
                if self.filterset is not None:
                    qs = self.filterset(request.GET, qs, request=request).qs
                pk_list = qs.only('pk').values_list('pk', flat=True)
            else:
                pk_list = request.POST.getlist('pk')
            mappings = self.queryset.filter(pk__in=pk_list)
            dependent_objects = [
                dependent
                for mapping in mappings
                for dependent in list(mapping.field_pairs.all()) + list(mapping.synced_objects.all())
            ]
            _report_protected_objects(request, mappings, dependent_objects)
            return redirect(self.get_return_url(request))


@register_model_view(SyncMapping, 'sync')
class SyncMappingSyncView(ObjectPermissionRequiredMixin, View):
    """POST-only "Sync now": enqueue a one-shot ``ReconcileJob``.

    The exact enqueue the Task 11 REST action
    (``POST /api/plugins/netbox_inventree_sync/mappings/<id>/sync/``)
    performs — a job with no interval that runs at the first available
    opportunity, in addition to any recurring schedule. The button
    itself is a small form on the mapping detail page
    (``syncmapping.html``).

    Permission gate: the mapping's *change* permission, not merely view
    — clicking it writes through the mapping to InvenTree, so a
    view-only user receives a 403 (and no job). GET is not served
    (405): there is nothing to show.
    """

    queryset = SyncMapping.objects.all()

    def get_required_permission(self):
        return 'netbox_inventree_sync.change_syncmapping'

    def post(self, request, *args, **kwargs):
        # self.queryset was restricted to permitted objects by the
        # permission mixin, so this also enforces object-level rules.
        mapping = get_object_or_404(self.queryset, pk=kwargs['pk'])
        ReconcileJob.enqueue(instance=mapping, mapping_id=mapping.pk, user=request.user)
        messages.success(request, _('Sync scheduled'))
        return redirect(mapping.get_absolute_url())


#
# Field-pair builder
#

# Bootstrap badge classes per field type, so both sides' type badges stay
# consistent between the pairs table and the add-pair selects.
_TYPE_BADGES = {
    'text': 'text-bg-primary',
    'string': 'text-bg-primary',
    'boolean': 'text-bg-info',
    'integer': 'text-bg-warning',
    'float': 'text-bg-warning',
    'date': 'text-bg-success',
    'pointer': 'text-bg-secondary',
    'related': 'text-bg-secondary',
}


def _badge_class(field_type):
    return _TYPE_BADGES.get(field_type, 'text-bg-dark')


@register_model_view(SyncMapping, 'builder')
class SyncMappingBuilderView(ObjectPermissionRequiredMixin, View):
    """The spec's two-column field-pair builder for one mapping.

    GET renders the mapping's current pairs as linked rows plus the
    fields matrix (the same data the Task 11 ``/fields/`` endpoint
    returns, fetched server-side and embedded as a JSON script block —
    the page needs no further auth hops). POST takes a JSON array of
    pairs, validates it through
    :func:`~netbox_inventree_sync.sync.validation.validate_mapping` and
    :func:`~netbox_inventree_sync.sync.validation.validate_mapping_graph_for_write`
    — the same gates the REST API applies; the client-side greying is UX,
    not enforcement — and on success replaces the mapping's full pair
    set inside one transaction, mirroring the serializer semantics.
    """

    queryset = SyncMapping.objects.all()
    template_name = 'netbox_inventree_sync/mapping_builder.html'

    def get_required_permission(self):
        # Viewing the builder needs view; saving pairs changes the mapping.
        if self.request.method == 'POST':
            return 'netbox_inventree_sync.change_syncmapping'
        return 'netbox_inventree_sync.view_syncmapping'

    def get_object(self):
        return get_object_or_404(self.queryset, pk=self.kwargs['pk'])

    # --- request handling ---------------------------------------------------

    def get(self, request, *args, **kwargs):
        mapping = self.get_object()
        try:
            fields_payload = self._fields_payload(mapping)
        except InvenTreeClientError as exc:
            # InvenTree unreachable: still render the page with the current
            # pairs (read-only rows) and an error banner, instead of a 500.
            messages.error(request, _('Could not load InvenTree field metadata: %(error)s') % {'error': exc})
            fields_payload = None
        return render(request, self.template_name, self._context(mapping, fields_payload))

    def post(self, request, *args, **kwargs):
        mapping = self.get_object()
        pairs_data, error = self._parse_pairs(request)
        fields_payload = None
        if error is None:
            try:
                fields_payload = self._fields_payload(mapping)
            except InvenTreeClientError as exc:
                error = str(exc)
        if error is not None:
            context = self._context(mapping, None, errors=[error])
            return render(request, self.template_name, context, status=400)

        # Unsaved pairs (never full_clean()ed — validate_mapping is the
        # gate); the mapping is the persisted one, exactly as the
        # serializer does for updates.
        pairs = [
            SyncFieldPair(
                mapping=mapping,
                netbox_field=entry['netbox_field'],
                netbox_subfield=entry.get('netbox_subfield') or '',
                inventree_field=entry['inventree_field'],
            )
            for entry in pairs_data
        ]
        try:
            validated_pairs = validate_mapping(mapping, pairs)
            # Same pairing-graph gate the REST serializer applies: a pair
            # set that closes a cycle is rejected inline, nothing written.
            validate_mapping_graph_for_write(mapping, validated_pairs)
        except DjangoValidationError as exc:
            context = self._context(
                mapping, fields_payload, posted_pairs=pairs,
                errors=[error_.message for error_ in exc.error_list],
            )
            return render(request, self.template_name, context, status=400)
        except InvenTreeClientError as exc:
            # Upstream failure while validating: nothing was the user's
            # fault, but the request cannot be fulfilled right now.
            context = self._context(
                mapping, fields_payload, posted_pairs=pairs,
                errors=[_('Could not reach InvenTree while validating: %(error)s') % {'error': exc}],
            )
            return render(request, self.template_name, context, status=502)

        with transaction.atomic():
            # Full replace (delete-then-create), mirroring the REST
            # serializer's nested field_pairs writes.
            mapping.field_pairs.all().delete()
            for pair in validated_pairs:
                pair.pk = None
                pair.mapping = mapping
                pair.save()

        messages.success(request, _('Saved %(count)d field pair(s) for "%(mapping)s".') % {
            'count': len(validated_pairs),
            'mapping': mapping,
        })
        return redirect(request.path)

    # --- context assembly ---------------------------------------------------

    def _context(self, mapping, fields_payload, posted_pairs=None, errors=None):
        """Template context: pairs (posted ones win on a failed save), the
        add-pair selects' options with their server-rendered compatibility
        state, and the embedded fields matrix."""
        pairs = list(posted_pairs) if posted_pairs is not None else list(mapping.field_pairs.all())
        netbox_by_name = {spec['name']: spec for spec in fields_payload['netbox']} if fields_payload else {}
        inventree_by_name = {spec['name']: spec for spec in fields_payload['inventree']} if fields_payload else {}
        context = {
            'object': mapping,
            'model': self.queryset.model,
            'errors': errors or [],
            'fields_data': fields_payload,
            'pair_rows': [
                {
                    'netbox_field': pair.netbox_field,
                    'netbox_label': netbox_by_name.get(pair.netbox_field, {}).get('label', pair.netbox_field),
                    'netbox_type': netbox_by_name.get(pair.netbox_field, {}).get('type', 'unknown'),
                    'netbox_badge': _badge_class(netbox_by_name.get(pair.netbox_field, {}).get('type', '')),
                    'netbox_subfield': pair.netbox_subfield,
                    'inventree_field': pair.inventree_field,
                    'inventree_type': inventree_by_name.get(pair.inventree_field, {}).get('type', 'unknown'),
                    'inventree_badge': _badge_class(inventree_by_name.get(pair.inventree_field, {}).get('type', '')),
                    'transform': pair.transform,
                }
                for pair in pairs
            ],
            'netbox_options': [],
            'inventree_options': [],
            'selected_netbox': '',
            'selected_netbox_subfields': [],
            'return_url': mapping.get_absolute_url(),
            'pairs_json': json.dumps([
                {
                    'netbox_field': pair.netbox_field,
                    'netbox_subfield': pair.netbox_subfield,
                    'inventree_field': pair.inventree_field,
                }
                for pair in pairs
            ]),
        }

        if fields_payload is None:
            return context

        context['netbox_options'] = [
            {
                'value': spec['name'],
                'label': spec['label'],
                'type': spec['type'],
                'badge': _badge_class(spec['type']),
                'subfields': ','.join(spec['subfield_options']),
            }
            for spec in fields_payload['netbox']
        ]

        # The add-pair row starts on the first text field (the most common
        # mapping; a model without any falls back to the first spec).
        selected = next(
            (spec for spec in fields_payload['netbox'] if spec['type'] == 'text'),
            fields_payload['netbox'][0] if fields_payload['netbox'] else None,
        )
        if selected is not None:
            context['selected_netbox'] = selected['name']
            context['selected_netbox_subfields'] = list(selected['subfield_options'])

        context['inventree_options'] = [
            self._inventree_option(spec, selected)
            for spec in fields_payload['inventree']
        ]
        return context

    @staticmethod
    def _inventree_option(spec, selected):
        """One InvenTree select option, greyed out with the mismatch tooltip
        when the pre-selected NetBox field is not in its compatible list."""
        disabled = selected is not None and selected['name'] not in spec['compatible_netbox_fields']
        return {
            'value': spec['name'],
            'label': spec['name'],
            'type': spec['type'],
            'badge': _badge_class(spec['type']),
            'compatible': ','.join(spec['compatible_netbox_fields']),
            'disabled': disabled,
            'title': f'type mismatch: {selected["type"]} → {spec["type"]}' if disabled else '',
        }

    # --- data assembly --------------------------------------------------------

    def _fields_payload(self, mapping):
        """The Task 11 fields matrix for this mapping's connection, content
        type and endpoint.

        Same shape as ``GET /api/plugins/netbox_inventree_sync/fields/``.
        The pairing set comes from validation's own helper, so the page's
        greying can never disagree with the POST-time verdicts (the
        mapping under edit counts as its own pairing when enabled).
        """
        netbox_specs = get_netbox_fields(mapping.netbox_content_type)
        available_pairings = _available_pairings(mapping)
        client = InvenTreeClient(mapping.connection.url, mapping.connection.api_token)
        inventree_specs = client.get_field_specs(mapping.inventree_endpoint)
        return {
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
        }

    @staticmethod
    def _parse_pairs(request):
        """Extract the JSON pair list from a form-encoded or JSON-body POST.

        Accepts ``pairs=<json>`` form data (the builder form) as well as a
        raw JSON body (a bare array or ``{"pairs": [...]}``). Returns
        ``(pairs_data, error)``; exactly one is None.
        """
        raw = request.POST.get('pairs')
        if raw is None and request.content_type == 'application/json':
            try:
                body = json.loads(request.body.decode() or 'null')
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None, 'Request body is not valid JSON.'
            raw = body if isinstance(body, list) else (body.get('pairs') if isinstance(body, dict) else None)
        if raw is None:
            return None, 'Missing "pairs": a JSON array of {netbox_field, netbox_subfield, inventree_field} objects.'
        try:
            pairs_data = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            return None, '"pairs" is not valid JSON.'
        if not isinstance(pairs_data, list):
            return None, '"pairs" must be a JSON array.'
        for entry in pairs_data:
            if not isinstance(entry, dict):
                return None, 'Each pair must be a JSON object.'
            for key in ('netbox_field', 'inventree_field'):
                if not isinstance(entry.get(key), str) or not entry[key].strip():
                    return None, f'Each pair needs a non-empty "{key}".'
            if not isinstance(entry.get('netbox_subfield', ''), str):
                return None, '"netbox_subfield" must be a string.'
        return pairs_data, None


#
# Read-only sync status lists
#


@register_model_view(SyncedObject, 'list', path='', detail=False)
class SyncedObjectListView(generic.ObjectListView):
    """Every tracked link row and its status (list is read-only)."""

    # select_related covers what the table's custom columns reach into
    # (the mapping, its endpoint and content type, the connection URL);
    # the mapped NetBox object itself is fetched per row — a primary-key
    # query bounded by the page size.
    queryset = SyncedObject.objects.select_related(
        'mapping', 'mapping__connection', 'mapping__netbox_content_type',
    )
    filterset = filtersets.SyncedObjectFilterSet
    filterset_form = forms.SyncedObjectFilterForm
    table = tables.SyncedObjectTable
    template_name = 'netbox_inventree_sync/syncedobject_list.html'
    # Read-only: link rows are managed by the sync engine, not by hand.
    actions = ()


@register_model_view(SyncLogEntry, 'list', path='', detail=False)
class SyncLogEntryListView(generic.ObjectListView):
    """The push-attempt audit trail (list is read-only)."""

    queryset = SyncLogEntry.objects.select_related('synced', 'synced__mapping')
    filterset = filtersets.SyncLogEntryFilterSet
    filterset_form = forms.SyncLogEntryFilterForm
    table = tables.SyncLogEntryTable
    template_name = 'netbox_inventree_sync/synclogentry_list.html'
    actions = ()
