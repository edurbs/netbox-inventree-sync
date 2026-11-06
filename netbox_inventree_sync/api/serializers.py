"""API serializers for the InvenTree sync plugin models.

Highlights:

* ``InvenTreeInstanceSerializer`` exposes the model's ``url`` column as
  ``server_url`` — the inherited ``url`` field is the object's API
  identity (NetBox convention) and would otherwise shadow it — and keeps
  ``api_token`` strictly write-only.
* ``SyncMappingSerializer`` carries a nested, writable ``field_pairs``
  list. Its ``validate()`` runs :func:`~netbox_inventree_sync.sync.validation.validate_mapping`
  on unsaved :class:`~netbox_inventree_sync.models.SyncFieldPair`
  instances built from the posted data, so an incompatible mapping is
  rejected with HTTP 400 (carrying the validator's error codes) instead
  of ever reaching the sync engine; on success each pair's
  ``transform`` is auto-set by the validator. When ``field_pairs`` is
  present on a write it replaces the mapping's full pair set; when
  absent (e.g. a PATCH) the existing pairs are left untouched.
"""

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework.exceptions import ErrorDetail, ValidationError
from rest_framework.settings import api_settings

from netbox.api.fields import ContentTypeField
from netbox.api.serializers import NetBoxModelSerializer
from netbox_inventree_sync.models import (
    InvenTreeInstance, SyncFieldPair, SyncLogEntry, SyncMapping, SyncedObject,
)
from netbox_inventree_sync.sync.validation import validate_mapping

__all__ = (
    'InvenTreeInstanceSerializer',
    'SyncFieldPairSerializer',
    'SyncLogEntrySerializer',
    'SyncMappingSerializer',
    'SyncedObjectSerializer',
)


class InvenTreeInstanceSerializer(NetBoxModelSerializer):
    server_url = serializers.URLField(
        source='url',
        label='Server URL',
        help_text="Base URL of the InvenTree server, e.g. https://inventree.example.com",
    )
    api_token = serializers.CharField(
        write_only=True,
        max_length=255,
        help_text='InvenTree API token; accepted on write, never returned on read',
    )

    class Meta:
        model = InvenTreeInstance
        fields = ('id', 'url', 'display', 'name', 'server_url', 'api_token', 'enabled')
        brief_fields = ('id', 'url', 'display', 'name', 'enabled')


class SyncFieldPairSerializer(serializers.ModelSerializer):
    """One NetBox → InvenTree field assignment of a mapping.

    Deliberately a plain DRF ``ModelSerializer``: NetBox's
    ``ValidatedModelSerializer`` would ``full_clean()`` a freshly built
    (unsaved, mapping-less) pair instance and reject it long before the
    mapping serializer's ``validate()`` can run ``validate_mapping``.
    Pairs are persisted by the mapping serializer, which is also why
    ``transform`` is read-only — it is computed by ``validate_mapping``.
    """

    class Meta:
        model = SyncFieldPair
        fields = ('id', 'netbox_field', 'netbox_subfield', 'inventree_field', 'transform')
        read_only_fields = ('id', 'transform')


class SyncMappingSerializer(NetBoxModelSerializer):
    connection = InvenTreeInstanceSerializer(nested=True)
    netbox_content_type = ContentTypeField(
        queryset=ContentType.objects.all(),
    )
    field_pairs = SyncFieldPairSerializer(
        many=True,
        required=False,
        help_text='Full pair set of the mapping; replaces all existing pairs when present',
    )

    class Meta:
        model = SyncMapping
        fields = (
            'id', 'url', 'display', 'name', 'connection', 'netbox_content_type',
            'inventree_endpoint', 'match_netbox_field', 'match_inventree_field',
            'reconcile_interval_minutes', 'enabled', 'description', 'field_pairs',
        )
        brief_fields = ('id', 'url', 'display', 'name', 'enabled')

    def validate(self, data):
        pairs_data = data.get('field_pairs')
        if pairs_data is not None:
            # Validate the posted pairs against live NetBox introspection and
            # the endpoint's OPTIONS metadata (the InvenTree side is stubbed
            # in tests via sync.validation.InvenTreeClient). The provisional
            # mapping mirrors NetBox's own ValidatedModelSerializer: unsaved
            # on create, the persisted instance patched with the posted attrs
            # on update.
            mapping = self.instance or SyncMapping()
            for name, value in data.items():
                if name != 'field_pairs':
                    setattr(mapping, name, value)
            pairs = [SyncFieldPair(**attrs) for attrs in pairs_data]
            try:
                validated_pairs = validate_mapping(mapping, pairs)
            except DjangoValidationError as exc:
                raise self._drf_validation_error(exc)
            data = {**data, 'field_pairs': validated_pairs}
        return super().validate(data)

    def create(self, validated_data):
        pairs = validated_data.pop('field_pairs', [])
        instance = super().create(validated_data)
        self._save_pairs(instance, pairs)
        return instance

    def update(self, instance, validated_data):
        pairs = validated_data.pop('field_pairs', None)
        instance = super().update(instance, validated_data)
        if pairs is not None:
            self._save_pairs(instance, pairs)
        return instance

    @staticmethod
    def _save_pairs(mapping, pairs):
        """Persist ``pairs`` as the mapping's complete pair set."""
        mapping.field_pairs.all().delete()
        for pair in pairs:
            pair.pk = None
            pair.mapping = mapping
            pair.save()

    @staticmethod
    def _drf_validation_error(exc):
        """Translate Django's coded ``ValidationError`` into a DRF one.

        Keeps both message and code per error, so API consumers can
        branch on the validator's codes (incompatible,
        duplicate_target, unknown_field, pairing_missing,
        subfield_required) the same way the UI will.
        """
        if hasattr(exc, 'error_dict'):
            details = {
                key: [ErrorDetail(str(error), code=error.code) for error in errors]
                for key, errors in exc.error_dict.items()
            }
        else:
            details = [ErrorDetail(str(error), code=error.code) for error in exc.error_list]
        return ValidationError({api_settings.NON_FIELD_ERRORS_KEY: details})


class SyncedObjectSerializer(NetBoxModelSerializer):
    mapping = SyncMappingSerializer(nested=True, read_only=True)

    class Meta:
        model = SyncedObject
        fields = (
            'id', 'url', 'display', 'mapping', 'netbox_object_id', 'inventree_pk',
            'status', 'last_synced_at', 'last_payload_hash', 'last_error',
        )
        brief_fields = ('id', 'url', 'display', 'netbox_object_id', 'status')


class SyncLogEntrySerializer(NetBoxModelSerializer):
    synced = SyncedObjectSerializer(nested=True, allow_null=True, required=False)

    class Meta:
        model = SyncLogEntry
        fields = (
            'id', 'url', 'display', 'synced', 'action', 'payload',
            'response_status', 'response_body', 'created',
        )
        brief_fields = ('id', 'url', 'display', 'action')


# --- fields endpoint (schema documentation) -----------------------------------


class MappableFieldsQuerySerializer(serializers.Serializer):
    """Query parameters of ``GET /fields/`` (OpenAPI documentation only)."""

    connection = serializers.IntegerField(help_text='ID of an enabled InvenTree instance')
    content_type = serializers.IntegerField(help_text='ID of the NetBox content type to map')
    endpoint = serializers.CharField(help_text='InvenTree endpoint path, e.g. /api/part/')


class NetboxFieldSpecSerializer(serializers.Serializer):
    """One mappable NetBox field, as returned by ``GET /fields/``."""

    name = serializers.CharField()
    label = serializers.CharField()
    type = serializers.ChoiceField(choices=('text', 'boolean', 'integer', 'float', 'date', 'pointer'))
    pointer_model = serializers.CharField(allow_null=True)
    custom = serializers.BooleanField()
    subfield_options = serializers.ListField(child=serializers.CharField())


class InventreeFieldSpecSerializer(serializers.Serializer):
    """One writable InvenTree field plus its compatible NetBox field names."""

    name = serializers.CharField()
    type = serializers.ChoiceField(choices=('string', 'boolean', 'integer', 'float', 'related'))
    required = serializers.BooleanField()
    max_length = serializers.IntegerField(allow_null=True)
    related_model = serializers.CharField(allow_null=True)
    compatible_netbox_fields = serializers.ListField(child=serializers.CharField())


class MappableFieldsResponseSerializer(serializers.Serializer):
    """Response body of ``GET /fields/`` — the mapping-builder matrix."""

    netbox = NetboxFieldSpecSerializer(many=True, read_only=True)
    inventree = InventreeFieldSpecSerializer(many=True, read_only=True)
    available_pairings = serializers.ListField(child=serializers.CharField(), read_only=True)
