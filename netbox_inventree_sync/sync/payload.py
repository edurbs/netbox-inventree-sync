"""NetBox → InvenTree value extraction for one field pair.

Pure value plumbing: :func:`extract_value` turns a
:class:`~netbox_inventree_sync.models.SyncFieldPair` plus a NetBox object
into the scalar InvenTree expects (``str | int | bool | float | None``) —
plain attributes, custom fields, joined tag slugs, pointer-as-text
projections and the two supported transforms.
"""

from __future__ import annotations

from decimal import Decimal

from netbox_inventree_sync.choices import TransformChoices


def extract_value(obj, pair) -> str | int | bool | float | None:
    """Extract the InvenTree-bound value of ``pair`` from NetBox object ``obj``.

    Dispatch, in order: ``tags`` (comma-joined tag slugs), custom fields
    (``obj.custom_field_data[name]``), pointer-as-text
    (``getattr(obj, netbox_field).netbox_subfield``; ``None`` when the
    pointer is empty) and plain attributes (``None`` when absent).
    Transforms: ``iso_date`` calls ``isoformat()`` on date-like values,
    ``str`` stringifies; both pass ``None`` through untouched.
    """
    name = pair.netbox_field
    if name == 'tags':
        value = ','.join(tag.slug for tag in obj.tags.all())
    elif name in (getattr(obj, 'custom_field_data', None) or {}):
        value = obj.custom_field_data.get(name)
    elif pair.netbox_subfield:
        target = getattr(obj, name, None)
        value = getattr(target, pair.netbox_subfield, None) if target is not None else None
    else:
        value = getattr(obj, name, None)

    if isinstance(value, Decimal):
        value = float(value)  # JSON-safe; DecimalField lands as a float

    if value is None:
        return None
    if pair.transform == TransformChoices.ISO_DATE:
        return value.isoformat() if hasattr(value, 'isoformat') else value
    if pair.transform == TransformChoices.STR:
        return str(value)
    return value
