"""Hermetic tests for the field type compatibility matrix.

Pure functions, no DB and no network: these tests pin the spec's matrix —
which NetBox→InvenTree type pairs may map, the pointer side conditions
(subfields for the as-text projection, an available pairing mapping for the
FK projection) and the default transform per accepted pair.
"""

import unittest
from typing import NamedTuple

from netbox_inventree_sync.sync.client import InventreeFieldSpec
from netbox_inventree_sync.sync.compatibility import compatible, transform_for
from netbox_inventree_sync.sync.fields import NetboxFieldSpec


def nb_spec(**overrides) -> NetboxFieldSpec:
    """A NetboxFieldSpec with ``text`` defaults, overridden per row."""
    defaults = dict(
        name='nb_field', label='Nb Field', type='text',
        pointer_model=None, subfield_options=[],
    )
    defaults.update(overrides)
    return NetboxFieldSpec(**defaults)


def inv_spec(**overrides) -> InventreeFieldSpec:
    """An InventreeFieldSpec with ``string`` defaults, overridden per row."""
    defaults = dict(
        name='inv_field', type='string', required=False,
        max_length=None, related_model=None,
    )
    defaults.update(overrides)
    return InventreeFieldSpec(**defaults)


class MatrixRow(NamedTuple):
    """One compatibility matrix row: inputs, verdict, default transform."""

    id: str
    nb: dict
    inv: dict
    pairings: frozenset
    compatible: bool
    transform: str


POINTER_NB = dict(
    type='pointer', pointer_model='dcim.manufacturer', subfield_options=['name'],
)

# The spec matrix in full: every accepted row plus every pair the spec
# explicitly rejects. 'decimal→float' in the spec is float→float here —
# Task 5 introspects DecimalField as the 'float' spec type.
MATRIX_ROWS = (
    MatrixRow(
        id='text->string',
        nb={'type': 'text'}, inv={'type': 'string'},
        pairings=frozenset(), compatible=True, transform='none',
    ),
    MatrixRow(
        id='boolean->string',
        nb={'type': 'boolean'}, inv={'type': 'string'},
        pairings=frozenset(), compatible=False, transform='none',
    ),
    MatrixRow(
        id='integer->integer',
        nb={'type': 'integer'}, inv={'type': 'integer'},
        pairings=frozenset(), compatible=True, transform='none',
    ),
    MatrixRow(
        id='float->float',
        nb={'type': 'float'}, inv={'type': 'float'},
        pairings=frozenset(), compatible=True, transform='none',
    ),
    MatrixRow(
        id='date->string',
        nb={'type': 'date'}, inv={'type': 'string'},
        pairings=frozenset(), compatible=True, transform='iso_date',
    ),
    MatrixRow(
        id='pointer->string',
        nb=dict(POINTER_NB), inv={'type': 'string'},
        pairings=frozenset(), compatible=True, transform='str',
    ),
    MatrixRow(
        id='pointer->related with pairing present',
        nb=dict(POINTER_NB), inv={'type': 'related', 'related_model': 'company'},
        pairings=frozenset({'dcim.manufacturer'}), compatible=True, transform='none',
    ),
    MatrixRow(
        id='pointer->related without pairing',
        nb=dict(POINTER_NB), inv={'type': 'related', 'related_model': 'company'},
        pairings=frozenset(), compatible=False, transform='none',
    ),
    MatrixRow(
        id='text->related',
        nb={'type': 'text'}, inv={'type': 'related', 'related_model': 'company'},
        pairings=frozenset({'dcim.manufacturer'}), compatible=False, transform='none',
    ),
    MatrixRow(
        id='text->boolean',
        nb={'type': 'text'}, inv={'type': 'boolean'},
        pairings=frozenset(), compatible=False, transform='none',
    ),
    MatrixRow(
        id='pointer with empty subfield_options -> string',
        nb={'type': 'pointer', 'pointer_model': 'dcim.manufacturer', 'subfield_options': []},
        inv={'type': 'string'},
        pairings=frozenset(), compatible=False, transform='str',
    ),
)


class CompatibilityMatrixTestCase(unittest.TestCase):
    """``compatible`` implements the spec matrix exactly, row by row."""

    def test_matrix_rows(self):
        for row in MATRIX_ROWS:
            with self.subTest(pair=row.id):
                nb = nb_spec(**row.nb)
                inv = inv_spec(**row.inv)
                result = compatible(nb, inv, set(row.pairings))
                self.assertEqual(result, row.compatible)
                if row.compatible:
                    self.assertEqual(transform_for(nb.type, inv.type), row.transform)

    def test_pointer_to_related_checks_pointer_model_not_field_name(self):
        # A different pointer target in the pairings set must not satisfy
        # this pointer's pairing requirement.
        nb = nb_spec(**POINTER_NB)
        inv = inv_spec(type='related', related_model='company')
        self.assertFalse(compatible(nb, inv, {'dcim.location'}))
        self.assertTrue(compatible(nb, inv, {'dcim.location', 'dcim.manufacturer'}))


class TransformForTestCase(unittest.TestCase):
    """``transform_for`` is a pure function of the two type names."""

    def test_date_to_string_is_iso_date(self):
        self.assertEqual(transform_for('date', 'string'), 'iso_date')

    def test_pointer_to_string_is_str(self):
        self.assertEqual(transform_for('pointer', 'string'), 'str')

    def test_scalar_matches_are_none(self):
        for nb_type, inv_type in [('text', 'string'), ('integer', 'integer'), ('float', 'float')]:
            with self.subTest(pair=(nb_type, inv_type)):
                self.assertEqual(transform_for(nb_type, inv_type), 'none')

    def test_pairs_outside_matrix_default_to_none(self):
        for nb_type, inv_type in [
            ('boolean', 'string'), ('boolean', 'boolean'),
            ('text', 'related'), ('text', 'boolean'),
            ('pointer', 'related'), ('date', 'integer'),
        ]:
            with self.subTest(pair=(nb_type, inv_type)):
                self.assertEqual(transform_for(nb_type, inv_type), 'none')
