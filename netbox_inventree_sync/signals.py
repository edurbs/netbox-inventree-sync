"""Signal wiring: NetBox object saves/deletes become ObjectSyncJob rows.

:func:`register_receivers` binds ``post_save``/``post_delete`` receivers to
the content types of every ENABLED mapping, and keeps those bindings
current through three triggers: every
:class:`~netbox_inventree_sync.models.SyncMapping` save (one cheap query
plus set arithmetic, fully idempotent), ``post_migrate`` (fresh or
just-migrated database), and the first request in a process (a server
restarting with existing enabled mappings — ``AppConfig.ready()`` may not
touch the database, so the initial binding build is deferred off the boot
path).

The binding set is only an event pre-filter: each receiver re-queries the
enabled mappings for the sender's content type at event time, so a stale
binding (a mapping disabled or deleted after it was made) can never enqueue
work — it only means a superfluous query.

Two hard invariants:

- **Nothing ever raises into NetBox's request path.** Every receiver wraps
  its whole body in try/except and logs; a failing enqueue is a log entry,
  not a broken save of a user's object.
- **No feedback loops.** The plugin's own models are skipped by app label:
  a ``SyncLogEntry`` save is a ``post_save`` too, and enqueuing on it would
  feed the engine its own output.
"""

import logging

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.core.signals import request_started
from django.db.models.signals import post_delete, post_migrate, post_save

from netbox.constants import RQ_QUEUE_DEFAULT
from netbox_inventree_sync.jobs import ObjectSyncJob
from netbox_inventree_sync.models import SyncMapping

__all__ = (
    'register_receivers',
    'unregister_receivers',
)

logger = logging.getLogger('netbox_inventree_sync.signals')

APP_LABEL = 'netbox_inventree_sync'

_MAPPING_SAVED_UID = f'{APP_LABEL}.mapping_saved'
_POST_MIGRATE_UID = f'{APP_LABEL}.post_migrate'
_REQUEST_STARTED_UID = f'{APP_LABEL}.request_started'

# Content-type id -> model class currently bound to object-sync receivers.
# Module state; rebuilt by _rebuild_bindings().
_bound: dict[int, type] = {}

# One-shot flag for the per-process bootstrap on the first request.
_bootstrapped = False


def register_receivers():
    """Connect all plugin receiver hooks; safe to call any number of times.

    Also builds the initial bindings — but only once Django has finished
    app initialization: querying the database during ``AppConfig.ready()``
    is forbidden (every ``manage.py`` command would raise Django's
    "Accessing the database during app initialization" warning). When
    called from ``ready()`` the hooks alone are connected and the bindings
    converge through any of three later triggers: ``post_migrate`` (migrate
    runs, test-database creation), the first request in the process (a
    server restarting with existing enabled mappings), or the next
    SyncMapping save.
    """
    post_migrate.connect(_handle_post_migrate, dispatch_uid=_POST_MIGRATE_UID)
    post_save.connect(_handle_mapping_saved, sender=SyncMapping, dispatch_uid=_MAPPING_SAVED_UID)
    request_started.connect(_handle_request_started, dispatch_uid=_REQUEST_STARTED_UID)
    if apps.ready:
        _rebuild_bindings()


def unregister_receivers():
    """Disconnect every receiver this module connected (idempotent)."""
    global _bootstrapped
    post_save.disconnect(_handle_mapping_saved, sender=SyncMapping, dispatch_uid=_MAPPING_SAVED_UID)
    post_migrate.disconnect(_handle_post_migrate, dispatch_uid=_POST_MIGRATE_UID)
    request_started.disconnect(_handle_request_started, dispatch_uid=_REQUEST_STARTED_UID)
    for content_type_id, model in _bound.items():
        post_save.disconnect(_handle_object_saved, sender=model, dispatch_uid=_save_uid(content_type_id))
        post_delete.disconnect(_handle_object_deleted, sender=model, dispatch_uid=_delete_uid(content_type_id))
    _bound.clear()
    _bootstrapped = False


def _save_uid(content_type_id):
    return f'{APP_LABEL}.object_sync.post_save.{content_type_id}'


def _delete_uid(content_type_id):
    return f'{APP_LABEL}.object_sync.post_delete.{content_type_id}'


def _rebuild_bindings():
    """Bind/unbind the object-sync receivers to match the enabled mappings."""
    enabled = set(
        SyncMapping.objects.filter(enabled=True)
        .values_list('netbox_content_type_id', flat=True)
        .distinct()
    )
    for content_type_id in set(_bound) - enabled:
        model = _bound.pop(content_type_id)
        post_save.disconnect(_handle_object_saved, sender=model, dispatch_uid=_save_uid(content_type_id))
        post_delete.disconnect(_handle_object_deleted, sender=model, dispatch_uid=_delete_uid(content_type_id))
    for content_type_id in enabled - set(_bound):
        model = ContentType.objects.get(pk=content_type_id).model_class()
        if model is None:
            logger.warning(
                f'content type #{content_type_id} of an enabled mapping has no '
                'model; not binding sync receivers for it'
            )
            continue
        post_save.connect(_handle_object_saved, sender=model, dispatch_uid=_save_uid(content_type_id))
        post_delete.connect(_handle_object_deleted, sender=model, dispatch_uid=_delete_uid(content_type_id))
        _bound[content_type_id] = model


def _is_plugin_model(sender) -> bool:
    """True when the signal's sender is one of the plugin's own models."""
    meta = getattr(sender, '_meta', None)
    return meta is not None and meta.app_label == APP_LABEL


def _enqueue_object_sync(sender, instance, *, deleted):
    """Enqueue one ObjectSyncJob per enabled mapping covering the sender."""
    content_type = ContentType.objects.get_for_model(sender)
    mappings = SyncMapping.objects.filter(
        enabled=True,
        netbox_content_type=content_type,
    )
    for mapping in mappings:
        # The Job row ties to the mapping (the plugin's only job-aware
        # model); the synced object rides in the job kwargs.
        ObjectSyncJob.enqueue(
            instance=mapping,
            queue_name=RQ_QUEUE_DEFAULT,
            mapping_id=mapping.pk,
            content_type_id=content_type.pk,
            object_id=instance.pk,
            deleted=deleted,
        )


# --- receivers (never raise) --------------------------------------------------


def _handle_object_saved(sender, instance, **kwargs):
    """post_save on a mapped model → ObjectSyncJob(deleted=False)."""
    try:
        if _is_plugin_model(sender):
            return
        _enqueue_object_sync(sender, instance, deleted=False)
    except Exception:
        pk = getattr(instance, 'pk', None)
        logger.exception(
            f'{APP_LABEL}: post_save receiver failed for {sender!r} #{pk}; '
            'the object was saved but no sync job was enqueued'
        )


def _handle_object_deleted(sender, instance, **kwargs):
    """post_delete on a mapped model → ObjectSyncJob(deleted=True)."""
    try:
        if _is_plugin_model(sender):
            return
        _enqueue_object_sync(sender, instance, deleted=True)
    except Exception:
        pk = getattr(instance, 'pk', None)
        logger.exception(
            f'{APP_LABEL}: post_delete receiver failed for {sender!r} #{pk}; '
            'the object was deleted but no deactivate job was enqueued'
        )


def _handle_mapping_saved(sender, instance, **kwargs):
    """post_save on SyncMapping → rebuild the object-sync bindings.

    Rebuilt on every mapping save, not only on enabled-flag changes: the
    rebuild is idempotent and cheap, and this also converges mappings
    whose content type changed or that were created by any code path.
    """
    try:
        _rebuild_bindings()
    except Exception:
        logger.exception(f'{APP_LABEL}: could not rebuild sync receivers after saving {instance!r}')


def _handle_post_migrate(sender, **kwargs):
    """post_migrate (this app) → re-register; the database now has our tables."""
    if getattr(sender, 'label', None) != APP_LABEL:
        return
    try:
        register_receivers()
    except Exception:
        logger.exception(f'{APP_LABEL}: post_migrate receiver registration failed')


def _handle_request_started(sender, **kwargs):
    """First request in this process → build the initial bindings.

    ``AppConfig.ready()`` may not touch the database and ``post_migrate``
    only fires on migrate runs, so a server that restarts with existing
    enabled mappings gets its bindings here, once per process; afterwards
    the SyncMapping save watcher keeps them current. (RQ worker processes
    never serve requests — and never need these bindings: they run jobs,
    whose engine writes only the plugin's own, guarded models.)
    """
    global _bootstrapped
    if _bootstrapped:
        return
    _bootstrapped = True
    try:
        _rebuild_bindings()
    except Exception:
        logger.exception(f'{APP_LABEL}: initial sync receiver registration failed')
