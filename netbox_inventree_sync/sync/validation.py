"""Mapping validation.

Wires the pieces produced by Tasks 3-6 into two entry points:
:func:`validate_mapping` checks one mapping's field pairs against live
NetBox introspection and the InvenTree endpoint's OPTIONS metadata,
auto-setting each pair's transform; :func:`validate_mapping_graph` checks
a connection's enabled mappings for pairing dependency cycles. All
failures raise :class:`django.core.exceptions.ValidationError` with one of
the codes ``incompatible``, ``duplicate_target``, ``unknown_field``,
``pairing_missing``, ``subfield_required`` or ``cycle``.
"""

from __future__ import annotations

from collections.abc import Iterable

from django.core.exceptions import ValidationError

from netbox_inventree_sync.models import SyncFieldPair, SyncMapping
from netbox_inventree_sync.sync.client import InvenTreeClient
from netbox_inventree_sync.sync.compatibility import compatible, transform_for
from netbox_inventree_sync.sync.fields import get_netbox_fields


def validate_mapping(mapping: SyncMapping, pairs: list[SyncFieldPair]) -> list[SyncFieldPair]:
    """Validate ``pairs`` for ``mapping``; return them with ``transform`` set.

    Every pair is checked against the NetBox field specs of the mapping's
    content type and the InvenTree field specs of its endpoint. All
    problems are collected into one ``ValidationError``; its ``error_list``
    carries one coded error per problem:

    * ``unknown_field`` — a field name missing from its side's specs,
    * ``duplicate_target`` — two pairs writing the same InvenTree field,
    * ``pairing_missing`` — pointer→related whose pairing mapping is
      absent or disabled on the connection,
    * ``subfield_required`` — pointer→string without ``netbox_subfield``,
    * ``incompatible`` — a pair rejected by the compatibility matrix.

    Pairs are not saved; callers persist the returned list.
    """
    netbox_specs = {spec.name: spec for spec in get_netbox_fields(mapping.netbox_content_type)}
    client = InvenTreeClient(mapping.connection.url, mapping.connection.api_token)
    inventree_specs = {
        spec.name: spec for spec in client.get_field_specs(mapping.inventree_endpoint)
    }
    available_pairings = _available_pairings(mapping)

    errors: list[ValidationError] = []
    seen_targets: set[str] = set()
    for pair in pairs:
        if pair.inventree_field in seen_targets:
            errors.append(ValidationError(
                f'Multiple pairs target InvenTree field "{pair.inventree_field}"; '
                'each InvenTree field may be written by a single pair.',
                code='duplicate_target',
            ))
        else:
            seen_targets.add(pair.inventree_field)

        netbox_spec = netbox_specs.get(pair.netbox_field)
        if netbox_spec is None:
            errors.append(ValidationError(
                f'NetBox field "{pair.netbox_field}" does not exist on '
                f'{_content_type_key(mapping.netbox_content_type)}.',
                code='unknown_field',
            ))
            continue
        inventree_spec = inventree_specs.get(pair.inventree_field)
        if inventree_spec is None:
            errors.append(ValidationError(
                f'InvenTree field "{pair.inventree_field}" does not exist on '
                f'{mapping.inventree_endpoint}.',
                code='unknown_field',
            ))
            continue

        if (
            netbox_spec.type == 'pointer' and inventree_spec.type == 'related'
            and netbox_spec.pointer_model not in available_pairings
        ):
            errors.append(ValidationError(
                f'Pair "{pair.netbox_field}" -> "{pair.inventree_field}" needs an enabled '
                f'mapping of {netbox_spec.pointer_model} on this connection (its pairing).',
                code='pairing_missing',
            ))
            continue
        if netbox_spec.type == 'pointer' and inventree_spec.type == 'string' and not pair.netbox_subfield:
            options = ', '.join(netbox_spec.subfield_options)
            errors.append(ValidationError(
                f'Pair "{pair.netbox_field}" -> "{pair.inventree_field}" projects a pointer '
                f'onto a string field; netbox_subfield is required (e.g. {options}).',
                code='subfield_required',
            ))
            continue
        if not compatible(netbox_spec, inventree_spec, available_pairings):
            errors.append(ValidationError(
                f'NetBox field "{pair.netbox_field}" ({netbox_spec.type}) is not compatible '
                f'with InvenTree field "{pair.inventree_field}" ({inventree_spec.type}).',
                code='incompatible',
            ))
            continue

        pair.transform = transform_for(netbox_spec.type, inventree_spec.type)

    if errors:
        raise ValidationError(errors)
    return list(pairs)


def validate_mapping_graph(mappings: Iterable[SyncMapping]) -> None:
    """Raise ``ValidationError`` (code ``cycle``) on pairing dependency cycles.

    Nodes are the enabled mappings of a connection (disabled mappings never
    run, so they cannot take part in a cycle); an edge M→P exists when M
    has a pointer→related pair whose pairing is P — that is, the pair's
    pointer target model is P's content type. A mapping pointing at its
    own content type is a self-edge and therefore a cycle too.
    """
    enabled = [mapping for mapping in mappings if mapping.enabled]
    by_connection: dict[int, list[SyncMapping]] = {}
    for mapping in enabled:
        by_connection.setdefault(mapping.connection_id, []).append(mapping)

    for group in by_connection.values():
        cycle_pks = _find_cycle(_pairing_edges(group))
        if cycle_pks is not None:
            by_pk = {mapping.pk: mapping for mapping in group}
            cycle = [by_pk[pk] for pk in cycle_pks]
            chain = ' -> '.join(mapping.name for mapping in cycle)
            raise ValidationError(
                f'Mapping pairing cycle: {chain} -> {cycle[0].name}.',
                code='cycle',
            )


# --- internals ----------------------------------------------------------------


def _available_pairings(mapping: SyncMapping) -> set[str]:
    """Content types ('app.model') of the connection's enabled mappings.

    The mapping under validation counts when it is enabled, even before
    being saved, so pre-save form validation matches the persisted case; a
    disabled mapping never offers itself as a pairing.
    """
    pairings = set(
        f'{app_label}.{model}'
        for app_label, model in SyncMapping.objects.filter(
            connection=mapping.connection,
            enabled=True,
        ).values_list('netbox_content_type__app_label', 'netbox_content_type__model')
    )
    if mapping.enabled:
        pairings.add(_content_type_key(mapping.netbox_content_type))
    return pairings


def _pairing_edges(group: list[SyncMapping]) -> dict[int, list[int]]:
    """Adjacency (pk -> neighbor pks) of pairing dependencies in one connection."""
    pairings_by_ct: dict[str, list[int]] = {}
    for mapping in group:
        key = _content_type_key(mapping.netbox_content_type)
        pairings_by_ct.setdefault(key, []).append(mapping.pk)

    edges: dict[int, list[int]] = {mapping.pk: [] for mapping in group}
    for mapping in group:
        netbox_specs = {spec.name: spec for spec in get_netbox_fields(mapping.netbox_content_type)}
        client = InvenTreeClient(mapping.connection.url, mapping.connection.api_token)
        inventree_specs = {
            spec.name: spec for spec in client.get_field_specs(mapping.inventree_endpoint)
        }
        for pair in mapping.field_pairs.all():
            netbox_spec = netbox_specs.get(pair.netbox_field)
            inventree_spec = inventree_specs.get(pair.inventree_field)
            if netbox_spec is None or inventree_spec is None:
                continue  # unknown fields are validate_mapping's business
            if netbox_spec.type == 'pointer' and inventree_spec.type == 'related':
                edges[mapping.pk].extend(pairings_by_ct.get(netbox_spec.pointer_model, []))
    return edges


def _find_cycle(edges: dict[int, list[int]]) -> list[int] | None:
    """Depth-first search over ``edges``; return the pks of one cycle, else ``None``.

    Three-colour DFS: a back edge to a node still on the recursion stack
    (grey) closes a cycle; self-edges are caught by the same rule.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    color = dict.fromkeys(edges, WHITE)
    path: list[int] = []

    def visit(pk: int) -> list[int] | None:
        color[pk] = GREY
        path.append(pk)
        for neighbor in edges[pk]:
            if color[neighbor] == GREY:
                return path[path.index(neighbor):]
            if color[neighbor] == WHITE:
                cycle = visit(neighbor)
                if cycle is not None:
                    return cycle
        path.pop()
        color[pk] = BLACK
        return None

    for pk in edges:
        if color[pk] == WHITE:
            cycle = visit(pk)
            if cycle is not None:
                return cycle
    return None


def _content_type_key(content_type) -> str:
    """``'app.model'`` natural key, matching :mod:`netbox_inventree_sync.sync.fields`."""
    return f'{content_type.app_label}.{content_type.model}'
