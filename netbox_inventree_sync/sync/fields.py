"""NetBox-side field introspection.

Turns a :class:`~django.contrib.contenttypes.models.ContentType` into a flat
list of :class:`NetboxFieldSpec` records describing every concrete, custom and
tag field available for mapping to InvenTree. Pure Django/NetBox metadata —
no InvenTree client or sync logic lives here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db import models

from extras.models import CustomField
from netbox.models.features import TagsMixin

# Django field class -> spec type. Matched with isinstance(), so subclasses
# inherit their parent's mapping (SlugField/EmailField/URLField under
# CharField -> text, DateTimeField under DateField -> date, the
# PositiveIntegerField family under IntegerField -> integer, OneToOneField
# under ForeignKey -> pointer).
FIELD_TYPE_MAP: tuple[tuple[type, str], ...] = (
    (models.CharField, 'text'),
    (models.TextField, 'text'),
    (models.BooleanField, 'boolean'),
    (models.IntegerField, 'integer'),
    (models.FloatField, 'float'),
    (models.DecimalField, 'float'),
    (models.DateField, 'date'),
    (models.ForeignKey, 'pointer'),
)

# CustomField.type -> spec type. The first group is the contract map; the
# second normalizes the remaining NetBox 4.7 CustomField types into the
# closest contract type so no active custom field is ever dropped.
CUSTOM_FIELD_TYPE_MAP: dict[str, str] = {
    'text': 'text',
    'longtext': 'text',
    'integer': 'integer',
    'boolean': 'boolean',
    'date': 'date',
    'url': 'text',
    'select': 'text',
    'object': 'pointer',
}
_CUSTOM_FIELD_TYPE_FALLBACKS: dict[str, str] = {
    'decimal': 'float',
    'datetime': 'date',
    'json': 'text',
    'multiselect': 'text',
    'multiobject': 'text',
}

MAX_SUBFIELD_OPTIONS = 5
PRIMARY_OPTION = 'name'


@dataclass(frozen=True)
class NetboxFieldSpec:
    """Normalized metadata for one mappable NetBox field.

    ``type`` is one of ``text|boolean|integer|float|date|pointer``. Pointer
    fields carry ``pointer_model`` as ``'<app>.<model>'`` and offer
    ``subfield_options`` — the text attributes of the pointer target that a
    mapping may project (e.g. ``['name']``). ``custom`` marks custom fields.
    """

    name: str
    label: str
    type: str
    pointer_model: str | None
    custom: bool = False
    subfield_options: list[str] = field(default_factory=list)


def get_netbox_fields(content_type) -> list[NetboxFieldSpec]:
    """Return every mappable field spec for ``content_type``'s model.

    Order is deterministic: concrete fields in model-definition order, then
    custom fields by name, then ``tags`` (models with tags only). Auto
    columns (primary key, ``created``/``last_updated``), reverse relations,
    many-to-many and generic foreign keys are never included — the first by
    explicit skip, the rest because ``_meta.concrete_fields`` excludes them.
    Concrete fields whose Django class has no known mapping (e.g. NetBox's
    internal ``custom_field_data`` JSONField) are skipped as unmappable.
    """
    model = content_type.model_class()

    specs = []
    for django_field in model._meta.concrete_fields:
        if _is_auto_field(django_field):
            continue
        spec_type = _map_django_type(django_field)
        if spec_type is None:
            continue  # unmappable internal column (JSONField, LtreeField, ...)
        specs.append(_spec_for_concrete_field(django_field, spec_type))

    specs.extend(_custom_field_specs(model))

    if issubclass(model, TagsMixin):
        # Tags attach as a manager, not a column; mapping consumers join
        # the assigned tag values into a single text value.
        specs.append(NetboxFieldSpec(
            name='tags',
            label='Tags',
            type='text',
            pointer_model=None,
        ))

    return specs


# --- internals ----------------------------------------------------------------


def _is_auto_field(django_field: models.Field) -> bool:
    return bool(
        django_field.primary_key
        or getattr(django_field, 'auto_now', False)
        or getattr(django_field, 'auto_now_add', False)
    )


def _map_django_type(django_field: models.Field) -> str | None:
    for field_class, spec_type in FIELD_TYPE_MAP:
        if isinstance(django_field, field_class):
            return spec_type
    return None


def _spec_for_concrete_field(django_field: models.Field, spec_type: str) -> NetboxFieldSpec:
    pointer_model = None
    subfield_options: list[str] = []
    if spec_type == 'pointer':
        target = django_field.related_model
        pointer_model = _content_type_key(target)
        subfield_options = _pointer_subfield_options(target)

    return NetboxFieldSpec(
        name=django_field.name,
        label=str(django_field.verbose_name).title(),
        type=spec_type,
        pointer_model=pointer_model,
        subfield_options=subfield_options,
    )


def _custom_field_specs(model) -> list[NetboxFieldSpec]:
    # get_for_model() is NetBox's canonical resolver: it filters to ACTIVE
    # fields (provisioning/retired fields stay invisible) and shares the
    # per-request cache.
    custom_fields = sorted(
        CustomField.objects.get_for_model(model),
        key=lambda cf: cf.name,
    )

    specs = []
    for custom_field in custom_fields:
        spec_type = CUSTOM_FIELD_TYPE_MAP.get(
            custom_field.type,
            _CUSTOM_FIELD_TYPE_FALLBACKS.get(custom_field.type, 'text'),
        )
        pointer_model = None
        subfield_options: list[str] = []
        if spec_type == 'pointer' and custom_field.related_object_type is not None:
            target = custom_field.related_object_type.model_class()
            pointer_model = _content_type_key(target)
            subfield_options = _pointer_subfield_options(target)

        specs.append(NetboxFieldSpec(
            name=custom_field.name,
            label=custom_field.label or custom_field.name,
            type=spec_type,
            pointer_model=pointer_model,
            custom=True,
            subfield_options=subfield_options,
        ))
    return specs


def _pointer_subfield_options(target) -> list[str]:
    """Text attributes of ``target`` offered as pointer projections.

    The target's non-nullable CharField names, sorted alphabetically with
    ``name`` promoted to the front when present, capped at
    :data:`MAX_SUBFIELD_OPTIONS`.
    """
    candidates = sorted(
        f.name
        for f in target._meta.concrete_fields
        if isinstance(f, models.CharField) and not f.null
    )
    if PRIMARY_OPTION in candidates:
        candidates.remove(PRIMARY_OPTION)
        candidates.insert(0, PRIMARY_OPTION)
    return candidates[:MAX_SUBFIELD_OPTIONS]


def _content_type_key(model) -> str:
    return f'{model._meta.app_label}.{model._meta.model_name}'
