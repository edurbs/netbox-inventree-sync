"""manage.py seed_starter_mappings — seed the plugin's starter mapping set.

Creates the design spec's starter templates (the catalog-mirror case) as
*disabled* SyncMappings on one InvenTree connection:

* ``dcim.inventoryitem``     -> ``/api/part/``
  (name->name, part_id->IPN, description->description,
   manufacturer projected on its ``name`` subfield -> keywords)
* ``dcim.manufacturer``      -> ``/api/company/`` (name->name, description->description)
* ``dcim.inventoryitemrole`` -> ``/api/part/category/`` (name->name)

Each mapping also gets name/name as its match (adopt) key, the canonical
identity pair for all three endpoints.

Idempotent: a mapping with the same (connection, netbox_content_type,
inventree_endpoint) — the model's unique constraint — is skipped untouched,
so re-running against a partly seeded connection only fills the gaps.

Validation semantics: by default the command never contacts the InvenTree
server (it must stay usable while InvenTree is down). Pairs are seeded with
sensible literal transforms — ``none`` for text pairs, ``str`` for the one
pointer-as-text pair (manufacturer.name -> keywords) — and a warning notes
that no live validation happened. With ``--validate``, every mapping's pairs
are checked through ``validate_mapping`` against the server's live OPTIONS
metadata; any incompatibility (or an unreachable server) aborts the whole
seed loudly with a non-zero exit, and nothing is persisted (the seed runs in
one transaction).
"""

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from netbox_inventree_sync.choices import TransformChoices
from netbox_inventree_sync.models import InvenTreeInstance, SyncFieldPair, SyncMapping
from netbox_inventree_sync.sync.client import InvenTreeClientError
from netbox_inventree_sync.sync.validation import validate_mapping

# (name, content type, endpoint, [(netbox_field, netbox_subfield, inventree_field), ...])
STARTER_MAPPINGS = [
    (
        'Inventory Item to Part',
        'dcim.inventoryitem',
        '/api/part/',
        [
            ('name', '', 'name'),
            ('part_id', '', 'IPN'),
            ('description', '', 'description'),
            ('manufacturer', 'name', 'keywords'),
        ],
    ),
    (
        'Manufacturer to Company',
        'dcim.manufacturer',
        '/api/company/',
        [
            ('name', '', 'name'),
            ('description', '', 'description'),
        ],
    ),
    (
        'Inventory Item Role to Part Category',
        'dcim.inventoryitemrole',
        '/api/part/category/',
        [
            ('name', '', 'name'),
        ],
    ),
]

STARTER_DESCRIPTION = 'Starter template mapping created by seed_starter_mappings.'


class Command(BaseCommand):
    help = (
        'Seed the spec starter set of disabled sync mappings (Inventory Item -> '
        '/api/part/, Manufacturer -> /api/company/, Inventory Item Role -> '
        '/api/part/category/) onto one InvenTree connection. Idempotent: '
        'existing mappings (same connection, content type and endpoint) are '
        'skipped. Pairs are seeded without contacting InvenTree (literal '
        "transforms: 'none' for text pairs, 'str' for the pointer-as-text "
        'manufacturer.name -> keywords pair); pass --validate to check every '
        'pair against the live server and abort the seed on incompatibility.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--connection',
            required=True,
            help='InvenTree instance to seed onto: its database ID or its name',
        )
        parser.add_argument(
            '--validate',
            action='store_true',
            help=(
                'Validate the starter pairs against the connection\'s live '
                'InvenTree OPTIONS metadata (via validate_mapping) before '
                'saving; incompatible or unreachable servers abort the seed '
                'with nothing persisted'
            ),
        )

    def handle(self, connection=None, validate=False, **options):
        instance = self._resolve_connection(connection)

        created = skipped = 0
        # One transaction: --validate failures must leave no half-seeded set.
        with transaction.atomic():
            for name, content_type_label, endpoint, pair_specs in STARTER_MAPPINGS:
                content_type = self._content_type(content_type_label)
                target = f'{content_type_label} -> {endpoint}'
                if SyncMapping.objects.filter(
                    connection=instance,
                    netbox_content_type=content_type,
                    inventree_endpoint=endpoint,
                ).exists():
                    self.stdout.write(f'Skipped {name!r} ({target}): mapping already exists')
                    skipped += 1
                    continue

                mapping = SyncMapping.objects.create(
                    name=name,
                    connection=instance,
                    netbox_content_type=content_type,
                    inventree_endpoint=endpoint,
                    match_netbox_field='name',
                    match_inventree_field='name',
                    enabled=False,
                    description=STARTER_DESCRIPTION,
                )
                pairs = [
                    SyncFieldPair(
                        mapping=mapping,
                        netbox_field=netbox_field,
                        netbox_subfield=netbox_subfield,
                        inventree_field=inventree_field,
                        # Offline defaults; --validate overwrites them from
                        # the live compatibility matrix below.
                        transform=(
                            TransformChoices.STR if netbox_subfield else TransformChoices.NONE
                        ),
                    )
                    for netbox_field, netbox_subfield, inventree_field in pair_specs
                ]
                if validate:
                    pairs = self._validate(mapping, pairs, name, target)
                mapping.field_pairs.bulk_create(pairs)

                self.stdout.write(self.style.SUCCESS(
                    f'Created {name!r} ({target}) with {len(pairs)} field pair(s)'
                ))
                created += 1

        if created and not validate:
            self.stdout.write(self.style.WARNING(
                'Seeded without live validation (InvenTree was not contacted): '
                "pairs carry literal transforms ('str' for manufacturer.name -> "
                "keywords, 'none' otherwise). Re-run with --validate, or use "
                'the field-pair builder, to check them against the server.'
            ))
        self.stdout.write(f'Done: {created} created, {skipped} skipped.')

    # --- helpers ---------------------------------------------------------------

    def _resolve_connection(self, ref) -> InvenTreeInstance:
        """Look the connection up by ID or name (ID preferred for digits)."""
        queryset = InvenTreeInstance.objects.all()
        if str(ref).isdigit():
            instance = queryset.filter(pk=int(ref)).first() or queryset.filter(name=ref).first()
        else:
            instance = queryset.filter(name=ref).first()
        if instance is None:
            raise CommandError(f'No InvenTree instance found with ID or name {ref!r}')
        return instance

    def _content_type(self, label) -> ContentType:
        app_label, _, model = label.partition('.')
        try:
            return ContentType.objects.get(app_label=app_label, model=model)
        except ContentType.DoesNotExist:
            raise CommandError(f'Unknown content type {label!r}')

    def _validate(self, mapping, pairs, name, target) -> list[SyncFieldPair]:
        """Run the pairs through validate_mapping; abort loudly on failure."""
        try:
            return validate_mapping(mapping, pairs)
        except ValidationError as exc:
            problems = '\n  '.join(f'- {message}' for message in exc.messages)
            raise CommandError(
                f'--validate: starter mapping {name!r} ({target}) is not '
                f'compatible with the InvenTree server:\n  {problems}'
            )
        except InvenTreeClientError as exc:
            raise CommandError(
                f'--validate: cannot validate {name!r} ({target}) against '
                f'{mapping.connection.url}: {exc}'
            )
