"""Signal, job and management-command wiring (hermetic: no HTTP, no worker).

The five pinned tests cover the event→job plumbing: saving a mapped NetBox
object enqueues an ``ObjectSyncJob`` row; a disabled mapping enqueues
nothing; a delete enqueues the deactivate job, and running that job inline
(``enqueue`` with ``immediate=True`` — the same ``JobRunner.handle → run``
path an RQ worker takes) drives the engine's deactivate path; saving an
enabled mapping schedules exactly one recurring ``ReconcileJob``; and the
``inventree_sync`` command runs the job classes inline with a fake client
injected at the ``jobs.build_engine`` seam. The extra tests pin the
command's default mode and failure exit code, the receiver lifecycle
(``unregister_receivers``), the never-raise invariant, and the
plugin-model feedback-loop skip.

A NetBox 4.7 constraint is baked into this design: ``Job`` rows may only be
attached to objects whose model carries the ``jobs`` feature
(``JobsMixin``) — ``Job.clean()`` rejects everything else. Both job classes
therefore tie their Job rows to the
:class:`~netbox_inventree_sync.models.SyncMapping` (which gains the
mixin); the synced NetBox object is identified by the job kwargs
(``mapping_id``/``object_id``) and recorded in ``job.data`` and on the
``SyncedObject`` link row.
"""

from io import StringIO
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.test import TestCase

from core.models import Job
from dcim.models import Manufacturer

from netbox_inventree_sync import signals
from netbox_inventree_sync.choices import SyncActionChoices, SyncStatusChoices
from netbox_inventree_sync.jobs import ObjectSyncJob, ReconcileJob
from netbox_inventree_sync.models import (
    InvenTreeInstance,
    SyncFieldPair,
    SyncLogEntry,
    SyncMapping,
    SyncedObject,
)
from netbox_inventree_sync.sync.client import InvenTreeClientError, InventreeFieldSpec
from netbox_inventree_sync.sync.engine import SyncEngine

from .test_engine import FakeClient

ENDPOINT = '/api/company/'


class DeactivatingFakeClient(FakeClient):
    """A FakeClient whose endpoint advertises a writable boolean 'active'."""

    def get_field_specs(self, endpoint):
        return [InventreeFieldSpec('active', 'boolean', False, None, None)]


class SignalAndJobTestCase(TestCase):
    """Event wiring: NetBox object saves/deletes → Job rows; mapping saves
    → recurring reconcile schedules. Job rows are never executed by a live
    worker here: the RQ enqueue is deferred to ``transaction.on_commit``,
    which a TestCase never reaches."""

    @classmethod
    def setUpTestData(cls):
        cls.connection = InvenTreeInstance.objects.create(
            name='Primary InvenTree',
            url='http://inventree.test',
            api_token='test-token-xyz',
        )
        cls.manufacturer_ct = ContentType.objects.get_for_model(Manufacturer)
        cls.mapping_ct = ContentType.objects.get_for_model(SyncMapping)

    def setUp(self):
        # Deterministic receiver state regardless of what earlier tests
        # (or the ambient post_migrate registration) left in the module.
        signals.register_receivers()
        self.fake = FakeClient()

    # --- fixtures -------------------------------------------------------------

    def make_mapping(self, *, enabled=True, interval=60):
        return SyncMapping.objects.create(
            name='Manufacturers to companies',
            connection=self.connection,
            netbox_content_type=self.manufacturer_ct,
            inventree_endpoint=ENDPOINT,
            reconcile_interval_minutes=interval,
            enabled=enabled,
        )

    def pair(self, mapping, netbox_field, inventree_field):
        return SyncFieldPair.objects.create(
            mapping=mapping,
            netbox_field=netbox_field,
            inventree_field=inventree_field,
        )

    def object_sync_jobs(self, mapping):
        return Job.objects.filter(
            name=ObjectSyncJob.name,
            object_type=self.mapping_ct,
            object_id=mapping.pk,
        )

    def make_manufacturer(self, name='Acme'):
        return Manufacturer.objects.create(name=name, slug=name.lower())

    # --- the pinned tests --------------------------------------------------------

    def test_save_signal_enqueues_job(self):
        mapping = self.make_mapping(enabled=True)

        mfr = self.make_manufacturer()

        jobs = self.object_sync_jobs(mapping)
        self.assertEqual(jobs.count(), 1)
        job = jobs.first()
        self.assertEqual(job.status, 'pending')
        self.assertEqual(job.queue_name, 'default')

    def test_disabled_mapping_no_job(self):
        mapping = self.make_mapping(enabled=False)

        mfr = self.make_manufacturer(name='Beta')

        self.assertEqual(self.object_sync_jobs(mapping).count(), 0)

    def test_delete_signal_deactivate_path(self):
        mapping = self.make_mapping(enabled=True)
        mfr = self.make_manufacturer()
        mfr_pk = mfr.pk  # captured before the delete nulls the instance pk
        SyncedObject.objects.create(
            mapping=mapping,
            netbox_object_id=mfr_pk,
            inventree_pk=101,
            status=SyncStatusChoices.SYNCED,
        )
        self.assertEqual(self.object_sync_jobs(mapping).count(), 1)  # from the save

        mfr.delete()  # post_delete → ObjectSyncJob(deleted=True) enqueued

        self.assertEqual(self.object_sync_jobs(mapping).count(), 2)

        # Run the enqueued job's logic inline through the production
        # JobRunner.handle → run path, with a fake engine injected.
        fake = DeactivatingFakeClient()
        with mock.patch(
            'netbox_inventree_sync.jobs.build_engine',
            side_effect=lambda connection: SyncEngine(connection, client=fake),
        ):
            job = ObjectSyncJob.enqueue(
                instance=mapping,
                mapping_id=mapping.pk,
                content_type_id=self.manufacturer_ct.pk,
                object_id=mfr_pk,
                deleted=True,
                immediate=True,
            )

        self.assertEqual(job.status, 'completed', getattr(job, 'error', None))
        self.assertEqual(fake.patches, [(ENDPOINT, 101, {'active': False})])
        synced = SyncedObject.objects.get(mapping=mapping, netbox_object_id=mfr_pk)
        self.assertEqual(synced.status, SyncStatusChoices.ORPHANED)
        # The remote pk is kept so the deactivated object stays locatable.
        self.assertEqual(synced.inventree_pk, 101)

    def test_mapping_save_schedules_reconcile(self):
        mapping = self.make_mapping(enabled=True, interval=45)

        jobs = Job.objects.filter(name=ReconcileJob.name, object_id=mapping.pk)
        self.assertEqual(jobs.count(), 1)
        self.assertEqual(jobs.first().interval, 45)

        # enqueue_once is idempotent: re-saving does not duplicate the schedule.
        mapping.save()
        self.assertEqual(jobs.count(), 1)

        # An interval change replaces the schedule instead of stacking.
        mapping.reconcile_interval_minutes = 30
        mapping.save()
        self.assertEqual(jobs.count(), 1)
        self.assertEqual(jobs.first().interval, 30)

        # Disabling the mapping removes the recurring schedule again.
        mapping.enabled = False
        mapping.save()
        self.assertEqual(jobs.count(), 0)

    def test_command_runs_inline(self):
        mapping = self.make_mapping(enabled=True)
        self.pair(mapping, 'name', 'name')
        mfr = self.make_manufacturer()

        output = StringIO()
        with mock.patch(
            'netbox_inventree_sync.jobs.build_engine',
            side_effect=lambda connection: SyncEngine(connection, client=self.fake),
        ):
            call_command(
                'inventree_sync', mapping=mapping.name,
                object=f'dcim.manufacturer:{mfr.pk}', stdout=output,
            )

        self.assertEqual(self.fake.posts, [(ENDPOINT, {'name': 'Acme'})])
        synced = SyncedObject.objects.get(mapping=mapping, netbox_object_id=mfr.pk)
        self.assertEqual(synced.status, SyncStatusChoices.SYNCED)
        self.assertEqual(synced.inventree_pk, 101)
        self.assertIn('synced=1', output.getvalue())

        # --reconcile variant: the synced link is verified against the remote.
        self.fake.get_response = {'pk': 101, 'name': 'Acme'}
        output = StringIO()
        with mock.patch(
            'netbox_inventree_sync.jobs.build_engine',
            side_effect=lambda connection: SyncEngine(connection, client=self.fake),
        ):
            call_command('inventree_sync', mapping=mapping.name, reconcile=True, stdout=output)

        self.assertEqual(self.fake.gets, [(ENDPOINT, 101)])
        synced.refresh_from_db()
        self.assertEqual(synced.status, SyncStatusChoices.SYNCED)
        self.assertIn('unchanged=1', output.getvalue())

    def test_command_default_mode_retries_failed_links(self):
        mapping = self.make_mapping(enabled=True)
        self.pair(mapping, 'name', 'name')
        mfr = self.make_manufacturer()
        SyncedObject.objects.create(
            mapping=mapping,
            netbox_object_id=mfr.pk,
            status=SyncStatusChoices.FAILED,
            last_error='previous failure',
        )

        output = StringIO()
        with mock.patch(
            'netbox_inventree_sync.jobs.build_engine',
            side_effect=lambda connection: SyncEngine(connection, client=self.fake),
        ):
            call_command('inventree_sync', mapping=mapping.name, stdout=output)

        self.assertEqual(self.fake.posts, [(ENDPOINT, {'name': 'Acme'})])
        synced = SyncedObject.objects.get(mapping=mapping, netbox_object_id=mfr.pk)
        self.assertEqual(synced.status, SyncStatusChoices.SYNCED)
        self.assertIn('synced=1', output.getvalue())

    def test_unregister_receivers_stops_enqueueing(self):
        mapping = self.make_mapping(enabled=True)

        signals.unregister_receivers()
        try:
            mfr = self.make_manufacturer(name='Gamma')
            self.assertEqual(self.object_sync_jobs(mapping).count(), 0)
        finally:
            signals.register_receivers()

    def test_receiver_failures_never_propagate(self):
        self.make_mapping(enabled=True)

        with mock.patch(
            'netbox_inventree_sync.signals._enqueue_object_sync',
            side_effect=RuntimeError('boom'),
        ):
            # The receiver must contain the failure: the save succeeds and
            # the error lands in the log, never in the request path.
            with self.assertLogs('netbox_inventree_sync.signals', level='ERROR'):
                mfr = self.make_manufacturer(name='Delta')

        self.assertIsNotNone(mfr.pk)

    def test_receiver_skips_plugin_models(self):
        # A SyncLogEntry save is a post_save too; feeding it back into the
        # engine would loop. The receivers must skip the plugin's own models
        # even when directly invoked.
        entry = SyncLogEntry.objects.create(
            action=SyncActionChoices.CREATE, payload={},
        )

        signals._handle_object_saved(SyncLogEntry, entry)
        signals._handle_object_deleted(SyncLogEntry, entry)

        self.assertEqual(Job.objects.filter(name=ObjectSyncJob.name).count(), 0)

    def test_command_exit_code_on_failure(self):
        mapping = self.make_mapping(enabled=True)
        self.pair(mapping, 'name', 'name')
        mfr = self.make_manufacturer()
        self.fake.post_error = InvenTreeClientError('InvenTree exploded', status=500)

        with mock.patch(
            'netbox_inventree_sync.jobs.build_engine',
            side_effect=lambda connection: SyncEngine(connection, client=self.fake),
        ):
            with self.assertRaises(SystemExit) as ctx:
                call_command(
                    'inventree_sync', mapping=mapping.name,
                    object=f'dcim.manufacturer:{mfr.pk}', stdout=StringIO(),
                )

        self.assertEqual(ctx.exception.code, 1)
        synced = SyncedObject.objects.get(mapping=mapping, netbox_object_id=mfr.pk)
        self.assertEqual(synced.status, SyncStatusChoices.FAILED)
