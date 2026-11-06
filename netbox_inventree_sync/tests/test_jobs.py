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
from dcim.models import Manufacturer, Region

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


class RecurringScheduleKwargsTestCase(TestCase):
    """The recurring ReconcileJob enqueued by ``SyncMapping.save()`` must carry
    its run kwargs (``mapping_id``) into the RQ entry.

    Regression pin: ``enqueue_once(instance, interval)`` alone stores no run
    kwargs, so the immediate first run (and every rescheduled recurrence —
    ``JobRunner.handle``'s finally-block forwards the same empty kwargs) died
    with ``TypeError: ReconcileJob.run() missing 1 required positional
    argument: 'mapping_id'`` the moment a worker picked it up.
    """

    def test_enabled_mapping_schedule_carries_run_kwargs(self):
        from core.choices import JobStatusChoices

        connection = InvenTreeInstance.objects.create(
            name='sched', url='http://x.test', api_token='tok'
        )
        mapping = SyncMapping(
            name='sched-mapping',
            connection=connection,
            netbox_content_type=ContentType.objects.get_for_model(Manufacturer),
            inventree_endpoint=ENDPOINT,
            enabled=True,
        )
        with self.captureOnCommitCallbacks(execute=True):
            mapping.save()

        job = (
            ReconcileJob.get_jobs(mapping)
            .filter(status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES)
            .first()
        )
        self.assertIsNotNone(job, 'enabling a mapping must enqueue a ReconcileJob')

        import django_rq

        rq_job = django_rq.get_queue(job.queue_name).fetch_job(str(job.job_id))
        self.assertIsNotNone(rq_job, 'RQ entry must exist for the enqueued job')
        self.assertEqual(rq_job.kwargs.get('mapping_id'), mapping.pk)


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

        # Pre-filter semantics: the disabled mapping's content type IS
        # bound (bindings are the union of all mapped content types), …
        self.assertIn(self.manufacturer_ct.pk, signals._bound)
        self.assertIs(signals._bound[self.manufacturer_ct.pk], Manufacturer)

        mfr = self.make_manufacturer(name='Beta')

        # … but the receiver's event-time re-check finds no enabled
        # mapping, so the save enqueues nothing.
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

    def test_mapped_plugin_model_save_never_enqueues(self):
        """The feedback-loop skip holds even when a mapping (wrongly)
        targets the plugin's own SyncLogEntry content type: the mapping
        save binds its receivers, but a SyncLogEntry save still enqueues
        zero ObjectSyncJobs."""
        log_ct = ContentType.objects.get_for_model(SyncLogEntry)
        SyncMapping.objects.create(
            name='Log entries to companies',
            connection=self.connection,
            netbox_content_type=log_ct,
            inventree_endpoint=ENDPOINT,
            enabled=True,
        )
        # Bound (it IS a mapped content type) — the skip must come from
        # the app-label guard, not from a missing binding.
        self.assertIn(log_ct.pk, signals._bound)

        SyncLogEntry.objects.create(action=SyncActionChoices.CREATE, payload={})

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

    def test_command_reconcile_job_error_exits_1(self):
        mapping = self.make_mapping(enabled=True)

        # run() blowing up before producing counters must exit 1 — an
        # errored job carries no stats, so a stats-only check would pass.
        errors = StringIO()
        with mock.patch.object(
            ReconcileJob, 'run', side_effect=RuntimeError('engine exploded'),
        ):
            with self.assertRaises(SystemExit) as ctx:
                call_command(
                    'inventree_sync', mapping=mapping.name, reconcile=True,
                    stdout=StringIO(), stderr=errors,
                )

        self.assertEqual(ctx.exception.code, 1)
        self.assertIn('engine exploded', errors.getvalue())


class RequestStartedRecheckTestCase(TestCase):
    """The request_started TTL re-check (not a one-shot bootstrap).

    A one-shot flag would permanently miss, on every other worker, a
    content type whose FIRST-EVER mapping was created in another process
    (the mapping-save rebuild runs only in the handling worker); the TTL
    re-check bounds that staleness window instead.
    """

    @classmethod
    def setUpTestData(cls):
        cls.connection = InvenTreeInstance.objects.create(
            name='Primary InvenTree', url='http://inventree.test', api_token='test-token-xyz',
        )

    def setUp(self):
        signals.register_receivers()

    def make_enabled_mapping_without_signals(self, content_type):
        """A mapping created the way ANOTHER process would create it:
        bulk_create skips post_save, so no local rebuild ever runs."""
        mapping = SyncMapping.objects.bulk_create([SyncMapping(
            name=f'{content_type.app_label}.{content_type.model} to companies',
            connection=self.connection,
            netbox_content_type=content_type,
            inventree_endpoint=ENDPOINT,
            enabled=True,
        )])[0]
        return mapping

    def test_request_started_binds_first_ever_mapping_created_elsewhere(self):
        region_ct = ContentType.objects.get_for_model(Region)
        self.make_enabled_mapping_without_signals(region_ct)
        self.assertNotIn(region_ct.pk, signals._bound)  # nothing bound it yet

        # TTL expired (fresh module state): the request re-check binds it…
        signals._bindings_checked_at = 0.0
        signals._handle_request_started(None)

        self.assertIn(region_ct.pk, signals._bound)
        self.assertIs(signals._bound[region_ct.pk], Region)

        # …and the binding actually fires: saving a Region enqueues a job.
        region = Region.objects.create(name='EMEA', slug='emea')
        mapping = SyncMapping.objects.get(netbox_content_type=region_ct)
        self.assertEqual(Job.objects.filter(
            name=ObjectSyncJob.name, object_id=mapping.pk,
        ).count(), 1)
        self.assertIsNotNone(region.pk)

    def test_request_started_rechecks_at_most_once_per_ttl(self):
        signals._bindings_checked_at = 0.0
        with mock.patch.object(
            signals, '_rebuild_bindings', wraps=signals._rebuild_bindings,
        ) as rebuild:
            signals._handle_request_started(None)
            first_checked_at = signals._bindings_checked_at

            # Inside the TTL: further requests must not re-query.
            signals._handle_request_started(None)
            signals._handle_request_started(None)
            self.assertEqual(rebuild.call_count, 1)
            self.assertEqual(signals._bindings_checked_at, first_checked_at)

            # Past the TTL the next request re-checks (one cheap query).
            signals._bindings_checked_at -= signals.BINDING_RECHECK_SECONDS + 1
            signals._handle_request_started(None)
            self.assertEqual(rebuild.call_count, 2)

    def test_request_started_failure_never_propagates(self):
        signals._bindings_checked_at = 0.0
        with mock.patch.object(
            signals, '_rebuild_bindings', side_effect=RuntimeError('db gone'),
        ):
            with self.assertLogs('netbox_inventree_sync.signals', level='ERROR'):
                signals._handle_request_started(None)  # must not raise
