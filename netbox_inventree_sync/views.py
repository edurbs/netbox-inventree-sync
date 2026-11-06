"""UI views: tutorial-style CRUD for connections and sync mappings.

Every view is registered with ``register_model_view()`` so the plugin's
``urls.py`` can wire them via ``get_model_urls()`` — the same mechanism
that automatically mounts NetBox's feature views (changelog, jobs) for
the models.
"""

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls.exceptions import NoReverseMatch
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from netbox.object_actions import AddObject, BulkDelete, BulkExport
from netbox.views import generic
from utilities.views import register_model_view

from . import filtersets, forms, tables
from .models import InvenTreeInstance, SyncMapping
from .sync.client import InvenTreeClient, InvenTreeClientError

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


def _report_protected_objects(request, obj, dependent_objects):
    """Report a blocked deletion without linking the dependent objects.

    NetBox's default handler links every protected object through its
    ``get_absolute_url()``; field pairs and synced objects carry such a
    method (model layer) but no routes until the builder/status screens
    arrive, so linking them would fail URL resolution. Plain names keep
    the delete views functional.
    """
    dependent_objects = list(dependent_objects)
    messages.error(
        request,
        mark_safe(
            _('Unable to delete <strong>{obj}</strong>. {count} dependent objects were found: {objects}').format(
                obj=escape(str(obj)),
                count=len(dependent_objects),
                objects=', '.join(escape(str(dependent)) for dependent in dependent_objects[:50]),
            )
        ),
    )
    return redirect(obj.get_absolute_url())


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
        return _report_protected_objects(request, obj, protected_objects)

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
            return _report_protected_objects(
                request, obj, list(obj.field_pairs.all()) + list(obj.synced_objects.all()),
            )


@register_model_view(SyncMapping, 'bulk_delete', path='delete', detail=False)
class SyncMappingBulkDeleteView(generic.BulkDeleteView):
    queryset = SyncMapping.objects.all()
    filterset = filtersets.SyncMappingFilterSet
    table = tables.SyncMappingTable
