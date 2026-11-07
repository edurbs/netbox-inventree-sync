from django.db.models import TextChoices


class SyncStatusChoices(TextChoices):
    """Link-table states for a tracked NetBox object."""

    PENDING = 'pending', 'Pending'
    SYNCED = 'synced', 'Synced'
    FAILED = 'failed', 'Failed'
    ORPHANED = 'orphaned', 'Orphaned'


class SyncActionChoices(TextChoices):
    """Actions recorded in the sync log."""

    CREATE = 'create', 'Create'
    UPDATE = 'update', 'Update'
    DEACTIVATE = 'deactivate', 'Deactivate'
    ADOPT = 'adopt', 'Adopt'
    ERROR = 'error', 'Error'


class TransformChoices(TextChoices):
    """Value transforms applied when pushing a NetBox field to InvenTree."""

    NONE = 'none', 'None'
    ISO_DATE = 'iso_date', 'ISO date'
    STR = 'str', 'String'
