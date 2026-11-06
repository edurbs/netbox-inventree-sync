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
    # Only models with change logging can be synced: the engine consumes
    # their change log entries. The plugin's own models and Django's
    # internal apps never carry that feature, so the feature query
    # already excludes them; the app_label exclusion is belt-and-braces
    # against future feature additions to the plugin's models.
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
