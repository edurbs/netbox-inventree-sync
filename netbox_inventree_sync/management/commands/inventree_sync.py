"""manage.py inventree_sync — deterministic, worker-free execution of sync jobs.

The E2E determinism tool. Instead of waiting for an RQ worker, the command
runs the exact production job classes (``ObjectSyncJob``/``ReconcileJob``)
synchronously in the foreground: ``enqueue`` with ``immediate=True``
executes ``JobRunner.handle → run → terminate`` right here, creating the
same ``Job`` rows and driving the same engine calls a worker would.

Modes (``--mapping`` is always required):

- ``--object <app>.<model>:<pk>`` — sync one NetBox object inline.
- ``--reconcile`` — one full reconcile pass over the mapping inline.
- neither — sync every ``pending``/``failed`` link of the mapping inline.

Exit code is 1 when any processed object ended ``failed`` (or a job itself
errored); 0 otherwise.
"""

import sys

from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count

from netbox.constants import RQ_QUEUE_DEFAULT
from netbox_inventree_sync.choices import SyncStatusChoices
from netbox_inventree_sync.jobs import ObjectSyncJob, ReconcileJob
from netbox_inventree_sync.models import SyncMapping


class Command(BaseCommand):
    help = (
        'Run InvenTree sync jobs for one mapping inline (no worker): a '
        'single object, a full reconcile, or every pending/failed link'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--mapping',
            required=True,
            help='Name of the sync mapping to run',
        )
        parser.add_argument(
            '--object',
            dest='object',
            metavar='APP.MODEL:PK',
            help='Sync a single NetBox object inline, e.g. dcim.manufacturer:12',
        )
        parser.add_argument(
            '--reconcile',
            action='store_true',
            help='Run one full reconcile pass for the mapping inline',
        )

    def handle(self, mapping=None, object=None, reconcile=False, **options):
        if not mapping:
            raise CommandError('--mapping is required')
        try:
            sync_mapping = SyncMapping.objects.select_related('connection').get(name=mapping)
        except SyncMapping.DoesNotExist:
            raise CommandError(f'No sync mapping named {mapping!r}')

        if object and reconcile:
            raise CommandError('--object and --reconcile are mutually exclusive')

        if reconcile:
            self._reconcile(sync_mapping)
            return

        if object:
            content_type, object_id = self._parse_object_ref(object)
            if content_type != sync_mapping.netbox_content_type:
                raise CommandError(
                    f'{content_type} does not match the mapping\'s content type '
                    f'({sync_mapping.netbox_content_type})'
                )
            processed_ids = [object_id]
            jobs = [self._sync_one(sync_mapping, object_id)]
        else:
            links = list(
                sync_mapping.synced_objects.filter(
                    status__in=(SyncStatusChoices.PENDING, SyncStatusChoices.FAILED),
                )
            )
            processed_ids = [link.netbox_object_id for link in links]
            jobs = [self._sync_one(sync_mapping, link.netbox_object_id) for link in links]

        self._report(sync_mapping, processed_ids)
        failed = sync_mapping.synced_objects.filter(
            netbox_object_id__in=processed_ids,
            status=SyncStatusChoices.FAILED,
        ).exists() or any(job.status != 'completed' for job in jobs)
        if failed:
            sys.exit(1)

    # --- helpers ---------------------------------------------------------------

    def _parse_object_ref(self, ref):
        """Parse ``<app>.<model>:<pk>`` into (ContentType, pk)."""
        try:
            ct_spec, _, pk = ref.rpartition(':')
            app_label, _, model = ct_spec.partition('.')
            if not app_label or not model:
                raise ValueError
            object_id = int(pk)
        except ValueError:
            raise CommandError(f'--object must look like <app>.<model>:<pk>, got {ref!r}')
        try:
            content_type = ContentType.objects.get(app_label=app_label, model=model)
        except ContentType.DoesNotExist:
            raise CommandError(f'Unknown content type {app_label}.{model}')
        return content_type, object_id

    def _sync_one(self, sync_mapping, object_id):
        """Run one ObjectSyncJob inline through the production job path."""
        job = ObjectSyncJob.enqueue(
            instance=sync_mapping,
            queue_name=RQ_QUEUE_DEFAULT,
            mapping_id=sync_mapping.pk,
            content_type_id=sync_mapping.netbox_content_type_id,
            object_id=object_id,
            deleted=False,
            immediate=True,
        )
        status = (job.data or {}).get('status', '?')
        self.stdout.write(
            f'Synced {sync_mapping.netbox_content_type} #{object_id} '
            f'({status}, job #{job.pk} {job.status})'
        )
        if job.status != 'completed':
            self.stderr.write(f'Job #{job.pk} {job.status}: {job.error}')
        return job

    def _reconcile(self, sync_mapping):
        """Run one ReconcileJob inline; report its outcome counters."""
        job = ReconcileJob.enqueue(
            instance=sync_mapping,
            mapping_id=sync_mapping.pk,
            immediate=True,
        )
        if job.status != 'completed':
            # run() blew up before producing counters; without this guard
            # an errored job (empty stats) would exit 0.
            self.stderr.write(f'Reconcile job #{job.pk} {job.status}: {job.error}')
            sys.exit(1)
        stats = (job.data or {}).get('stats', {})
        self.stdout.write(
            f'Reconciled {sync_mapping.name!r}: '
            + ', '.join(f'{outcome}={count}' for outcome, count in stats.items())
        )
        if stats.get('failed'):
            sys.exit(1)

    def _report(self, sync_mapping, processed_ids):
        """Print the resulting link-row status counts of the processed objects."""
        if not processed_ids:
            self.stdout.write(f'No pending or failed links for {sync_mapping.name!r}; nothing to do.')
            return
        counts = dict(
            sync_mapping.synced_objects
            .filter(netbox_object_id__in=processed_ids)
            .values_list('status')
            .annotate(n=Count('pk'))
            .order_by()
        )
        summary = ', '.join(f'{status}={count}' for status, count in sorted(counts.items()))
        self.stdout.write(f'Processed {len(processed_ids)} object(s): {summary}')
