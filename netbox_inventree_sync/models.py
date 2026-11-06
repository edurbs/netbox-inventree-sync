from django.db import models
from django.urls import reverse

from netbox.models import NetBoxModel
from netbox.models.features import JobsMixin

from .choices import SyncActionChoices, SyncStatusChoices, TransformChoices


class InvenTreeInstance(NetBoxModel):
    """
    A remote InvenTree server that NetBox pushes inventory data to.
    """

    name = models.CharField(
        max_length=100,
    )
    url = models.URLField()
    api_token = models.CharField(
        max_length=255,
    )
    enabled = models.BooleanField(
        default=True,
    )

    class Meta:
        ordering = ('name',)
        verbose_name = 'InvenTree instance'
        verbose_name_plural = 'InvenTree instances'

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('plugins:netbox_inventree_sync:inventreeinstance', args=[self.pk])


class SyncMapping(JobsMixin, NetBoxModel):
    """
    A mapping from one NetBox content type to one InvenTree endpoint.

    Carries the ``jobs`` feature (like NetBox's own DataSource) so the
    plugin's ObjectSyncJob/ReconcileJob rows can be attached to it:
    NetBox's ``Job.clean()`` rejects Job rows tied to any model without
    that feature, and arbitrary mapped models (dcim.site, ...) never have
    it. The individual synced objects are identified by each job's
    ``object_id`` argument and tracked on ``SyncedObject`` rows.
    """

    name = models.CharField(
        max_length=100,
    )
    connection = models.ForeignKey(
        to=InvenTreeInstance,
        on_delete=models.PROTECT,
    )
    netbox_content_type = models.ForeignKey(
        to='contenttypes.ContentType',
        on_delete=models.PROTECT,
    )
    inventree_endpoint = models.CharField(
        max_length=200,
        help_text="InvenTree REST endpoint, e.g. /api/part/",
    )
    match_netbox_field = models.CharField(
        max_length=100,
        blank=True,
    )
    match_inventree_field = models.CharField(
        max_length=100,
        blank=True,
    )
    reconcile_interval_minutes = models.PositiveSmallIntegerField(
        default=60,
    )
    enabled = models.BooleanField(
        default=False,
    )
    description = models.TextField(
        blank=True,
    )

    class Meta:
        ordering = ('name',)
        verbose_name = 'sync mapping'
        verbose_name_plural = 'sync mappings'
        constraints = [
            models.UniqueConstraint(
                fields=('connection', 'netbox_content_type', 'inventree_endpoint'),
                name='%(app_label)s_%(class)s_unique_endpoint',
            ),
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('plugins:netbox_inventree_sync:syncmapping', args=[self.pk])

    def save(self, *args, **kwargs):
        """Save, then maintain the recurring reconcile schedule.

        An enabled mapping keeps exactly one recurring ``ReconcileJob``
        schedule: ``enqueue_once`` is idempotent against any still-enqueued
        job and adopts interval changes (replacing a stale schedule).
        Saving the mapping disabled removes the schedule again — mirroring
        how NetBox handles a DataSource's ``sync_interval`` — so a disabled
        mapping does not keep reconciling forever.

        The jobs module is imported locally: it imports this module, so a
        module-level import would be circular.
        """
        super().save(*args, **kwargs)
        from netbox_inventree_sync.jobs import ReconcileJob
        if self.enabled:
            ReconcileJob.enqueue_once(instance=self, interval=self.reconcile_interval_minutes)
        else:
            self._cancel_reconcile_schedule()

    def _cancel_reconcile_schedule(self):
        """Delete any still-enqueued recurring reconcile jobs of this mapping."""
        from core.choices import JobStatusChoices
        from netbox_inventree_sync.jobs import ReconcileJob
        for job in ReconcileJob.get_jobs(self).filter(
            interval__isnull=False,
            status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES,
        ):
            # delete() per instance so the associated RQ entry is cancelled too
            job.delete()


class SyncFieldPair(NetBoxModel):
    """
    A single NetBox-to-InvenTree field assignment belonging to a sync mapping.
    """

    mapping = models.ForeignKey(
        to=SyncMapping,
        on_delete=models.PROTECT,
        related_name='field_pairs',
    )
    netbox_field = models.CharField(
        max_length=100,
    )
    netbox_subfield = models.CharField(
        max_length=100,
        blank=True,
        help_text="Pointer attribute when netbox_field is a text field (e.g. as-text)",
    )
    inventree_field = models.CharField(
        max_length=100,
    )
    transform = models.CharField(
        max_length=20,
        choices=TransformChoices,
        default=TransformChoices.NONE,
    )

    class Meta:
        ordering = ('netbox_field', 'inventree_field')
        verbose_name = 'field pair'
        verbose_name_plural = 'field pairs'

    def __str__(self):
        return f'{self.mapping}: {self.netbox_field} -> {self.inventree_field}'

    def get_absolute_url(self):
        return reverse('plugins:netbox_inventree_sync:syncfieldpair', args=[self.pk])


class SyncedObject(NetBoxModel):
    """
    Links one NetBox object to its remote InvenTree counterpart for a mapping.
    """

    mapping = models.ForeignKey(
        to=SyncMapping,
        on_delete=models.PROTECT,
        related_name='synced_objects',
    )
    netbox_object_id = models.PositiveBigIntegerField()
    inventree_pk = models.PositiveIntegerField(
        blank=True,
        null=True,
    )
    status = models.CharField(
        max_length=20,
        choices=SyncStatusChoices,
        default=SyncStatusChoices.PENDING,
    )
    last_synced_at = models.DateTimeField(
        blank=True,
        null=True,
    )
    last_payload_hash = models.CharField(
        max_length=64,
        blank=True,
    )
    last_error = models.TextField(
        blank=True,
    )

    class Meta:
        ordering = ('mapping', 'netbox_object_id')
        verbose_name = 'synced object'
        verbose_name_plural = 'synced objects'
        constraints = [
            models.UniqueConstraint(
                fields=('mapping', 'netbox_object_id'),
                name='%(app_label)s_%(class)s_unique_netbox_object',
            ),
        ]

    def __str__(self):
        return f'{self.mapping}: object #{self.netbox_object_id}'

    def get_absolute_url(self):
        return reverse('plugins:netbox_inventree_sync:syncedobject', args=[self.pk])


class SyncLogEntry(NetBoxModel):
    """
    One push attempt (or error) against InvenTree for a synced object.
    """

    synced = models.ForeignKey(
        to=SyncedObject,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='log_entries',
    )
    action = models.CharField(
        max_length=20,
        choices=SyncActionChoices,
    )
    payload = models.JSONField(
        default=dict,
    )
    response_status = models.PositiveSmallIntegerField(
        blank=True,
        null=True,
    )
    response_body = models.TextField(
        blank=True,
    )

    class Meta:
        ordering = ('-created',)
        verbose_name = 'log entry'
        verbose_name_plural = 'log entries'

    def __str__(self):
        return f'{self.get_action_display()} ({self.created})'

    def get_absolute_url(self):
        return reverse('plugins:netbox_inventree_sync:synclogentry', args=[self.pk])
