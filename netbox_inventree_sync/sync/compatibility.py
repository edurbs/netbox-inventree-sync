"""Field type compatibility between NetBox and InvenTree.

Implements the spec's compatibility matrix as a single table: rows are
(netbox type, inventree type) pairs that may be mapped, with the value
giving the default :class:`~netbox_inventree_sync.models.SyncFieldPair`
transform. Pure functions only — no I/O, no Django imports here — so
validation, the REST API and the mapping builder can all consume the same
matrix (see ``netbox_inventree_sync.tests.test_compatibility``).
"""

from __future__ import annotations

from netbox_inventree_sync.sync.client import InventreeFieldSpec
from netbox_inventree_sync.sync.fields import NetboxFieldSpec

# (netbox type, inventree type) -> default transform. A pair absent from
# the table is incompatible. The two pointer rows carry side conditions
# enforced by :func:`compatible`: pointer→string projects a text attribute
# (requires non-empty ``subfield_options``), pointer→related requires the
# pointer target to have an enabled pairing mapping (``available_pairings``).
_COMPATIBILITY_MATRIX: dict[tuple[str, str], str] = {
    ('text', 'string'): 'none',
    ('integer', 'integer'): 'none',
    ('float', 'float'): 'none',
    ('date', 'string'): 'iso_date',
    ('pointer', 'string'): 'str',
    ('pointer', 'related'): 'none',
}


def compatible(
    nb: NetboxFieldSpec,
    inv: InventreeFieldSpec,
    available_pairings: set[str],
) -> bool:
    """Whether a NetBox field may map onto an InvenTree field.

    Implements the spec matrix exactly. Beyond the type table, pointer
    fields are restricted per target type: pointer→string needs a non-empty
    ``subfield_options`` list (the projected text attribute) and
    pointer→related needs ``nb.pointer_model`` in ``available_pairings``
    (the content types of the connection's enabled mappings). Every pair
    outside the matrix — e.g. boolean→string, text→related — is
    incompatible.
    """
    if (nb.type, inv.type) not in _COMPATIBILITY_MATRIX:
        return False
    if nb.type == 'pointer':
        if inv.type == 'string' and not nb.subfield_options:
            return False
        if inv.type == 'related' and nb.pointer_model not in available_pairings:
            return False
    return True


def transform_for(nb_type: str, inv_type: str) -> str:
    """Default transform for a type pair: ``'iso_date'`` for date→string,
    ``'str'`` for pointer→string (as-text projection), else ``'none'``.
    """
    return _COMPATIBILITY_MATRIX.get((nb_type, inv_type), 'none')
