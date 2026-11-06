"""Background jobs carrying the sync engine through NetBox's job machinery.

Two :class:`~netbox.jobs.JobRunner` subclasses: ``ObjectSyncJob`` pushes
(or, on delete, deactivates) one mapped NetBox object, and ``ReconcileJob``
drives one full
:meth:`~netbox_inventree_sync.sync.engine.SyncEngine.reconcile_mapping`
pass for a mapping. Both build their engine through :func:`build_engine`
— the single seam tests use to inject an engine with a fake client — and
both attach their ``Job`` row to the ``SyncMapping`` (the only plugin
model carrying the ``jobs`` feature; NetBox's ``Job.clean()`` rejects Job
rows tied to models without it). The synced NetBox object is identified
by the job's ``mapping_id``/``content_type_id``/``object_id`` kwargs and
recorded in ``job.data`` and on the ``SyncedObject`` link row.

Jobs run on the ``default`` queue: the plugin registers no queue of its
own.
"""

from netbox.jobs import JobRunner

from netbox_inventree_sync.models import SyncMapping
from netbox_inventree_sync.sync.engine import SyncEngine


def build_engine(connection):
    """Construct the sync engine for one connection.

    Module-level seam: tests monkeypatch this to inject a
    :class:`~netbox_inventree_sync.sync.engine.SyncEngine` built around a
    fake client instead of a live HTTP connection.
    """
    return SyncEngine(connection)


class ObjectSyncJob(JobRunner):
    """Push one mapped NetBox object, or deactivate its remote counterpart.

    ``deleted=False`` runs the engine's ``sync_object`` (create/adopt/
    update as needed); ``deleted=True`` runs ``handle_delete`` (link goes
    ``orphaned``, the remote object is deactivated — never deleted).
    """

    class Meta:
        name = 'InvenTree Sync: Object'

    def run(self, mapping_id, content_type_id, object_id, deleted=False, **kwargs):
        mapping = SyncMapping.objects.select_related('connection').get(pk=mapping_id)
        engine = build_engine(mapping.connection)
        if deleted:
            synced = engine.handle_delete(mapping, object_id)
        else:
            synced = engine.sync_object(mapping, object_id)
        self.job.data = {
            'mapping': mapping.name,
            'netbox_object': f'{mapping.netbox_content_type.app_label}.'
                             f'{mapping.netbox_content_type.model}:{object_id}',
            'status': synced.status,
            'inventree_pk': synced.inventree_pk,
        }
        verb = 'Deactivated' if deleted else 'Synced'
        self.logger.info(
            f'{verb} {mapping.netbox_content_type} #{object_id} under {mapping.name!r}: '
            f'{synced.status}'
        )


class ReconcileJob(JobRunner):
    """Re-verify and repair every link of one mapping against InvenTree.

    Runs the engine's authoritative three-pass reconcile (verify synced
    links and repair drift, retry pending/failed links and publish
    unlinked objects, orphan links whose NetBox object is gone) and
    records the per-outcome counters on ``job.data``.
    """

    class Meta:
        name = 'InvenTree Sync: Reconcile'

    def run(self, mapping_id, **kwargs):
        mapping = SyncMapping.objects.select_related('connection').get(pk=mapping_id)
        engine = build_engine(mapping.connection)
        stats = engine.reconcile_mapping(mapping)
        self.job.data = {'mapping': mapping.name, 'stats': stats}
        summary = ', '.join(f'{outcome}={count}' for outcome, count in stats.items())
        self.logger.info(f'Reconciled {mapping.name!r}: {summary}')
