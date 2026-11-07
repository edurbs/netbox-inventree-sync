"""UI forms for connections, sync mappings and the read-only sync lists."""

from django import forms
from django.utils.translation import gettext_lazy as _

from core.models import ObjectType
from netbox.forms import NetBoxModelFilterSetForm, NetBoxModelForm
from utilities.forms.fields import (
    ContentTypeChoiceField, DynamicModelChoiceField, DynamicModelMultipleChoiceField,
)
from utilities.forms.rendering import FieldSet

from .choices import SyncActionChoices, SyncStatusChoices
from .models import InvenTreeInstance, SyncLogEntry, SyncMapping, SyncedObject

__all__ = (
    'InvenTreeInstanceForm',
    'InvenTreeInstanceFilterForm',
    'SyncLogEntryFilterForm',
    'SyncMappingForm',
    'SyncMappingFilterForm',
    'SyncedObjectFilterForm',
)


class InvenTreeInstanceForm(NetBoxModelForm):
    api_token = forms.CharField(
        widget=forms.PasswordInput(render_value=False),
        label=_('API token'),
        help_text=_('InvenTree API token'),
    )
    fieldsets = (
        FieldSet('name', 'url', 'api_token', 'enabled', name=_('Connection')),
    )

    class Meta:
        model = InvenTreeInstance
        fields = ('name', 'url', 'api_token', 'enabled')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Required to create a connection; on edit the token is never
        # prefilled and leaving it blank keeps the stored one.
        if self.instance.pk:
            self.fields['api_token'].required = False
            self.fields['api_token'].initial = None
            self.fields['api_token'].help_text = _('Leave blank to keep the existing token')
        else:
            self.fields['api_token'].required = True

    def clean(self):
        # NetBox mixins may return None from clean(); self.cleaned_data
        # is always populated at this point.
        cleaned_data = super().clean() or self.cleaned_data
        if self.instance.pk and not cleaned_data.get('api_token'):
            cleaned_data['api_token'] = self.instance.api_token
        return cleaned_data


class InvenTreeInstanceFilterForm(NetBoxModelFilterSetForm):
    model = InvenTreeInstance
    fieldsets = (
        FieldSet('q', 'filter_id', name=None),
    )


class SyncMappingForm(NetBoxModelForm):
    connection = DynamicModelChoiceField(
        queryset=InvenTreeInstance.objects.all(),
        label=_('Connection'),
    )
    # Only models with change logging can be synced (they are the ones a
    # real inventory workflow edits — and change logging keeps their
    # history inspectable). The plugin's own models DO carry that
    # NetBoxModel feature (SyncMapping change-logs too), so the
    # app_label exclusion below IS load-bearing — without it the plugin's
    # own models would be offered as sync targets. The signal layer
    # would still refuse to enqueue for them (no feedback loops), but a
    # mapping onto them could never do anything useful.
    netbox_content_type = ContentTypeChoiceField(
        queryset=ObjectType.objects.with_feature('change_logging').exclude(
            app_label='netbox_inventree_sync',
        ),
        label=_('NetBox object type'),
    )
    fieldsets = (
        FieldSet('name', 'connection', 'netbox_content_type', 'inventree_endpoint', name=_('Mapping')),
        FieldSet(
            'match_netbox_field', 'match_inventree_field', 'reconcile_interval_minutes', 'enabled',
            'description', name=_('Matching & Scheduling'),
        ),
    )

    class Meta:
        model = SyncMapping
        fields = (
            'name', 'connection', 'netbox_content_type', 'inventree_endpoint',
            'match_netbox_field', 'match_inventree_field', 'reconcile_interval_minutes',
            'enabled', 'description',
        )
        # Field pairs are edited on the mapping's builder screen, not here.

    def clean(self):
        """Gate enabling (or creating enabled) on the pairing-cycle check.

        The REST serializer and the builder run
        ``validate_mapping_graph_for_write`` on every graph-changing
        write; this form is the UI's equivalent surface and must not be
        the loophole. Disabled mappings are not graph nodes, so the gate
        only matters when the submitted state is enabled.
        """
        from django.core.exceptions import ValidationError as DjangoValidationError

        from netbox_inventree_sync.sync.client import InvenTreeClientError
        from netbox_inventree_sync.sync.validation import validate_mapping_graph_for_write

        # NetBox mixins may return None from clean(); self.cleaned_data
        # is always populated at this point.
        cleaned_data = super().clean() or self.cleaned_data
        if not cleaned_data.get('enabled'):
            return cleaned_data

        # Reflect the submitted state onto the instance for the graph
        # walk (node membership depends on `enabled`); the stored pairs
        # of an existing mapping are the pending pair set on this form.
        self.instance.enabled = True
        try:
            validate_mapping_graph_for_write(self.instance)
        except DjangoValidationError as exc:
            raise forms.ValidationError(exc.messages, code='cycle')
        except InvenTreeClientError as exc:
            # The gate needs the endpoint's live field metadata; report
            # the outage instead of accepting an unvalidated enable.
            raise forms.ValidationError(
                _('Cannot validate the pairing graph against InvenTree: %(error)s') % {'error': exc},
            )
        return cleaned_data


class SyncMappingFilterForm(NetBoxModelFilterSetForm):
    model = SyncMapping
    connection_id = DynamicModelMultipleChoiceField(
        queryset=InvenTreeInstance.objects.all(),
        required=False,
        label=_('Connection'),
    )
    fieldsets = (
        FieldSet('q', 'filter_id', name=None),
        FieldSet('connection_id', name=_('Mapping')),
    )


class SyncedObjectFilterForm(NetBoxModelFilterSetForm):
    model = SyncedObject
    status = forms.MultipleChoiceField(
        choices=SyncStatusChoices,
        required=False,
        label=_('Status'),
    )
    mapping_id = DynamicModelMultipleChoiceField(
        queryset=SyncMapping.objects.all(),
        required=False,
        label=_('Mapping'),
    )
    fieldsets = (
        FieldSet('q', 'filter_id', name=None),
        FieldSet('status', 'mapping_id', name=_('Sync')),
    )


class SyncLogEntryFilterForm(NetBoxModelFilterSetForm):
    model = SyncLogEntry
    action = forms.MultipleChoiceField(
        choices=SyncActionChoices,
        required=False,
        label=_('Action'),
    )
    synced_id = DynamicModelMultipleChoiceField(
        queryset=SyncedObject.objects.all(),
        required=False,
        label=_('Synced object'),
    )
    fieldsets = (
        FieldSet('q', 'filter_id', name=None),
        FieldSet('action', 'synced_id', name=_('Log')),
    )
