"""Mapping validation.

Wires the pieces produced by Tasks 3-6 into two entry points:
:func:`validate_mapping` checks one mapping's field pairs against live
NetBox introspection and the InvenTree endpoint's OPTIONS metadata,
auto-setting each pair's transform; :func:`validate_mapping_graph` checks
a connection's enabled mappings for pairing dependency cycles (the write
paths reach it through :func:`validate_mapping_graph_for_write`, which
substitutes the mapping being saved with its pending pair set). All
failures raise :class:`django.core.exceptions.ValidationError` with one
of the codes ``incompatible``, ``duplicate_target``, ``unknown_field``,
``pairing_missing``, ``pairing_ambiguous``, ``subfield_required`` or
``cycle``.
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
    * ``pairing_ambiguous`` — pointer→related whose target model is
      paired by MORE THAN ONE enabled mapping on the connection
      (spec: pointer resolution requires exactly one),
    * ``subfield_required`` — pointer→string without ``netbox_subfield``,
    * ``incompatible`` — a pair rejected by the compatibility matrix.

    Pairs are not saved; callers persist the returned list.
    """
    netbox_specs = {spec.name: spec for spec in get_netbox_fields(mapping.netbox_content_type)}
    client = InvenTreeClient(mapping.connection.url, mapping.connection.api_token)
    inventree_specs = {
        spec.name: spec for spec in client.get_field_specs(mapping.inventree_endpoint)
    }
    pairings_by_ct = _enabled_pairings_by_ct(mapping)
    available_pairings = set(pairings_by_ct)

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

        if netbox_spec.type == 'pointer' and inventree_spec.type == 'related':
            # Pointer resolution requires EXACTLY ONE enabled pairing
            # mapping for the target model on this connection.
            candidates = pairings_by_ct.get(netbox_spec.pointer_model, [])
            if not candidates:
                errors.append(ValidationError(
                    f'Pair "{pair.netbox_field}" -> "{pair.inventree_field}" needs an enabled '
                    f'mapping of {netbox_spec.pointer_model} on this connection (its pairing).',
                    code='pairing_missing',
                ))
                continue
            if len(candidates) > 1:
                errors.append(ValidationError(
                    f'Pair "{pair.netbox_field}" -> "{pair.inventree_field}" is ambiguous: '
                    f'{len(candidates)} enabled mappings ({", ".join(sorted(candidates))}) pair '
                    f'{netbox_spec.pointer_model} on this connection; exactly one is required.',
                    code='pairing_ambiguous',
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


def validate_mapping_graph(mappings: Iterable[SyncMapping], pair_overrides: dict | None = None) -> None:
    """Raise ``ValidationError`` (code ``cycle``) on pairing dependency cycles.

    Nodes are the enabled mappings of a connection (disabled mappings never
    run, so they cannot take part in a cycle); an edge M→P exists when M
    has a pointer→related pair whose pairing is P — that is, the pair's
    pointer target model is P's content type. A mapping pointing at its
    own content type is a self-edge and therefore a cycle too.

    ``pair_overrides`` optionally maps a mapping pk (``None`` for an
    unsaved mapping) to the pair list that mapping is ABOUT to receive;
    the write paths use it so a not-yet-saved pair set participates in
    the graph instead of the stale persisted one.
    """
    enabled = [mapping for mapping in mappings if mapping.enabled]
    by_connection: dict[int, list[SyncMapping]] = {}
    for mapping in enabled:
        by_connection.setdefault(mapping.connection_id, []).append(mapping)

    for group in by_connection.values():
        # One client for the whole walk of one connection: its OPTIONS
        # cache then serves each endpoint once no matter how many
        # mappings target it.
        client = InvenTreeClient(group[0].connection.url, group[0].connection.api_token)
        cycle_pks = _find_cycle(_pairing_edges(group, client, pair_overrides))
        if cycle_pks is not None:
            by_pk = {mapping.pk: mapping for mapping in group}
            cycle = [by_pk[pk] for pk in cycle_pks]
            chain = ' -> '.join(mapping.name for mapping in cycle)
            raise ValidationError(
                f'Mapping pairing cycle: {chain} -> {cycle[0].name}.',
                code='cycle',
            )


def validate_mapping_graph_for_write(mapping: SyncMapping, pairs: list[SyncFieldPair] | None = None) -> None:
    """Cycle-check the graph a mapping write would produce.

    Runs :func:`validate_mapping_graph` over the connection's enabled
    mappings with ``mapping`` standing in for its persisted self — the
    persisted row (if any) is excluded and the provisional mapping takes
    its place, its pair set being ``pairs`` when the write carries one
    and the stored set otherwise. A mapping under validation that is
    (or ends up) disabled is not a graph node and only the remaining
    graph is checked.
    """
    others = SyncMapping.objects.filter(
        connection=mapping.connection,
        enabled=True,
    ).exclude(pk=mapping.pk)
    if pairs is not None:
        pair_overrides = {mapping.pk: pairs}
    elif mapping.pk is None:
        # Unsaved and no pairs in the write: an unsaved mapping has no
        # stored pairs either — stand in with an empty set instead of
        # touching its related manager (Django raises ValueError on a
        # related filter against pk=None).
        pair_overrides = {mapping.pk: []}
    else:
        pair_overrides = None
    validate_mapping_graph([*others, mapping], pair_overrides=pair_overrides)


# --- internals ----------------------------------------------------------------


def _enabled_pairings_by_ct(mapping: SyncMapping) -> dict[str, list[str]]:
    """``'app.model'`` -> names of the connection's enabled mappings of that type.

    The mapping under validation counts exactly once when it is enabled
    (its persisted self is excluded from the query), even before being
    saved, so pre-save validation matches the persisted case; a disabled
    mapping never offers itself as a pairing. Callers needing only
    membership use :func:`_available_pairings`; callers deciding whether
    a pointer is resolvable need the counts (exactly one is required).
    """
    pairings: dict[str, list[str]] = {}
    rows = SyncMapping.objects.filter(
        connection=mapping.connection,
        enabled=True,
    ).exclude(pk=mapping.pk).values_list(
        'name',
        'netbox_content_type__app_label',
        'netbox_content_type__model',
    )
    for name, app_label, model in rows:
        pairings.setdefault(f'{app_label}.{model}', []).append(name)
    if mapping.enabled:
        pairings.setdefault(_content_type_key(mapping.netbox_content_type), []).append(mapping.name)
    return pairings


def _available_pairings(mapping: SyncMapping) -> set[str]:
    """Content types ('app.model') of the connection's enabled mappings.

    The mapping under validation counts when it is enabled, even before
    being saved, so pre-save form validation matches the persisted case; a
    disabled mapping never offers itself as a pairing.
    """
    return set(_enabled_pairings_by_ct(mapping))


def _pairing_edges(group: list[SyncMapping], client, pair_overrides: dict | None = None) -> dict[int, list[int]]:
    """Adjacency (pk -> neighbor pks) of pairing dependencies in one connection.

    ``client`` is shared across the whole group (see
    :func:`validate_mapping_graph`); ``pair_overrides`` replaces a
    mapping's stored pairs with its pending ones.
    """
    pairings_by_ct: dict[str, list[int]] = {}
    for mapping in group:
        key = _content_type_key(mapping.netbox_content_type)
        pairings_by_ct.setdefault(key, []).append(mapping.pk)

    edges: dict[int, list[int]] = {mapping.pk: [] for mapping in group}
    for mapping in group:
        netbox_specs = {spec.name: spec for spec in get_netbox_fields(mapping.netbox_content_type)}
        inventree_specs = {
            spec.name: spec for spec in client.get_field_specs(mapping.inventree_endpoint)
        }
        if pair_overrides and mapping.pk in pair_overrides:
            pairs = pair_overrides[mapping.pk]
        else:
            pairs = mapping.field_pairs.all()
        for pair in pairs:
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
