"""The sync engine: pushes mapped NetBox objects to one InvenTree server.

One :class:`SyncEngine` drives pushes for a single
:class:`~netbox_inventree_sync.models.InvenTreeInstance`. Per object the
engine claims the link row first (``get_or_create`` + ``select_for_update``
inside one transaction — the unique ``(mapping, netbox_object_id)`` makes
races safe and the row lock serializes concurrent jobs for the duration of
the HTTP calls), builds the full payload from the mapping's field pairs,
short-circuits when the payload hash is unchanged, and otherwise PATCHes
the linked remote object or publishes a new one — adopting an exactly
matching remote object when the mapping carries match fields. Every
per-object failure is contained: it lands on the link row
(``status``/``last_error``) and in an ``error`` log entry, never in the
caller's lap. Log text is truncated and stripped of connection secrets.

Pointer→related field pairs (a pointer field without a text subfield) are
deliberately skipped while building payloads: resolving them through
pairing mappings is not part of the core engine yet.
"""

from __future__ import annotations

import hashlib
import json

import requests
from django.db import transaction
from django.utils import timezone

from netbox_inventree_sync.choices import SyncActionChoices, SyncStatusChoices
from netbox_inventree_sync.models import SyncFieldPair, SyncLogEntry, SyncedObject
from netbox_inventree_sync.sync.client import InvenTreeClient, InvenTreeClientError
from netbox_inventree_sync.sync.fields import NetboxFieldSpec, get_netbox_fields
from netbox_inventree_sync.sync.payload import extract_value

# Cap for any response/error text persisted on a log entry or the link row.
MAX_LOG_TEXT = 2000

# reconcile_mapping outcome counters (one per outcome string below).
OUTCOMES = ('created', 'updated', 'adopted', 'failed', 'orphaned', 'unchanged')


def canonical_hash(payload: dict) -> str:
    """sha256 hex digest of the payload's canonical JSON (sorted keys)."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


class SyncEngine:
    """Pushes NetBox objects to the InvenTree server of one connection."""

    def __init__(self, connection, client=None):
        self.connection = connection
        self.client = client if client is not None else InvenTreeClient(
            connection.url, connection.api_token,
        )
        # ContentType id -> {field name: spec}; introspection is per-mapping
        # constant for the lifetime of one engine (one job).
        self._netbox_spec_cache: dict[int, dict[str, NetboxFieldSpec]] = {}

    # --- public API -----------------------------------------------------------

    def sync_object(self, mapping, netbox_object_id) -> SyncedObject:
        """Push one NetBox object; returns its (claimed or existing) link row."""
        return self._sync(mapping, netbox_object_id)[0]

    def reconcile_mapping(self, mapping) -> dict:
        """Sync every object in ``mapping``'s scope; return outcome counts.

        Scope is the mapping's existing link rows (pending/failed retried,
        synced objects refreshed via the hash fast-path) plus every NetBox
        object of the mapped content type without a link (published through
        the mapping's match/adopt policy).
        """
        stats = dict.fromkeys(OUTCOMES, 0)
        linked_ids = list(
            mapping.synced_objects.values_list('netbox_object_id', flat=True)
        )
        for netbox_object_id in linked_ids:
            stats[self._sync(mapping, netbox_object_id)[1]] += 1
        model = mapping.netbox_content_type.model_class()
        if model is not None:
            for netbox_obj in model.objects.exclude(pk__in=linked_ids):
                stats[self._sync(mapping, netbox_obj.pk)[1]] += 1
        return stats

    def deactivate(self, synced) -> None:
        """Mark ``synced`` orphaned (its NetBox object is gone) and log it.

        Local state transition only: remote cleanup is not part of the core
        engine. ``inventree_pk`` is kept so the remote object stays locatable.
        """
        with transaction.atomic():
            locked = SyncedObject.objects.select_for_update().get(pk=synced.pk)
            locked.status = SyncStatusChoices.ORPHANED
            locked.save()
            self._log(locked, SyncActionChoices.DEACTIVATE, {})
        synced.refresh_from_db()

    # --- claim & drive ----------------------------------------------------------

    def _sync(self, mapping, netbox_object_id) -> tuple[SyncedObject, str]:
        """Claim, then push one object; never raises client/transport errors.

        The row lock taken by :meth:`_claim` is held for the whole block —
        including the HTTP calls — so a second job arriving mid-push blocks
        on the lock and afterwards takes the update/no-op path.
        """
        with transaction.atomic():
            synced = self._claim(mapping, netbox_object_id)
            payload = None
            try:
                netbox_obj = self._fetch_netbox_object(mapping, netbox_object_id)
                if netbox_obj is None:
                    outcome = self._fail(
                        synced,
                        f'NetBox object #{netbox_object_id} of '
                        f'{mapping.netbox_content_type} no longer exists; nothing to push.',
                    )
                else:
                    payload = self._build_payload(mapping, netbox_obj)
                    outcome = self._push(mapping, synced, netbox_obj, payload)
            except (InvenTreeClientError, requests.RequestException) as exc:
                outcome = self._record_failure(synced, exc, payload)
        return synced, outcome

    def _claim(self, mapping, netbox_object_id) -> SyncedObject:
        """get_or_create the link row, then re-fetch it under a row lock."""
        SyncedObject.objects.get_or_create(
            mapping=mapping, netbox_object_id=netbox_object_id,
        )
        return SyncedObject.objects.select_for_update().get(
            mapping=mapping, netbox_object_id=netbox_object_id,
        )

    def _fetch_netbox_object(self, mapping, netbox_object_id):
        """The live NetBox object, or ``None`` when it is gone (or the
        mapping's content type is stale)."""
        model = mapping.netbox_content_type.model_class()
        if model is None:
            return None
        try:
            return model.objects.get(pk=netbox_object_id)
        except model.DoesNotExist:
            return None

    def _build_payload(self, mapping, netbox_obj) -> dict:
        """The full InvenTree payload for one object, built before any send."""
        specs = self._netbox_specs(mapping)
        payload = {}
        for pair in mapping.field_pairs.all():
            spec = specs.get(pair.netbox_field)
            if spec is not None and spec.type == 'pointer' and not pair.netbox_subfield:
                continue  # pointer→related: resolved via pairing mappings, not here
            payload[pair.inventree_field] = extract_value(netbox_obj, pair)
        return payload

    def _netbox_specs(self, mapping) -> dict[str, NetboxFieldSpec]:
        ct_id = mapping.netbox_content_type_id
        if ct_id not in self._netbox_spec_cache:
            self._netbox_spec_cache[ct_id] = {
                spec.name: spec for spec in get_netbox_fields(mapping.netbox_content_type)
            }
        return self._netbox_spec_cache[ct_id]

    # --- remote writes ----------------------------------------------------------

    def _push(self, mapping, synced, netbox_obj, payload) -> str:
        """Dispatch on link state: update, adopt-or-create, or create."""
        digest = canonical_hash(payload)
        if synced.inventree_pk is not None:
            return self._update(mapping, synced, payload, digest)
        if mapping.match_netbox_field and mapping.match_inventree_field:
            return self._adopt_or_create(mapping, synced, netbox_obj, payload, digest)
        return self._create(mapping, synced, payload, digest)

    def _update(self, mapping, synced, payload, digest) -> str:
        """PATCH the FULL payload onto the linked remote object.

        Hash fast-path: a link that is already ``synced`` with an unchanged
        payload hash costs no HTTP at all.
        """
        if synced.status == SyncStatusChoices.SYNCED and synced.last_payload_hash == digest:
            return 'unchanged'
        response = self.client.patch(mapping.inventree_endpoint, synced.inventree_pk, payload)
        self._mark_synced(synced, digest)
        self._log(synced, SyncActionChoices.UPDATE, payload, response_body=response)
        return 'updated'

    def _adopt_or_create(self, mapping, synced, netbox_obj, payload, digest) -> str:
        """Apply the adoption policy via the mapping's match key.

        Zero candidates → create; exactly one → adopt (store the link and
        PATCH the drift); more than one or a blank key → fail without
        posting — the engine never guesses.
        """
        # Unsaved transient pair: reuses the one extraction rule set.
        key = extract_value(netbox_obj, SyncFieldPair(
            mapping=mapping,
            netbox_field=mapping.match_netbox_field,
            inventree_field=mapping.match_inventree_field,
        ))
        if key is None or (isinstance(key, str) and not key.strip()):
            return self._fail(
                synced,
                f'blank match key: {mapping.match_netbox_field!r} is empty on '
                f'{mapping.netbox_content_type} #{netbox_obj.pk}; refusing to guess.',
                payload=payload,
            )
        candidates = self.client.match(
            mapping.inventree_endpoint, mapping.match_inventree_field, key,
        )
        if len(candidates) > 1:
            return self._fail(
                synced,
                f'ambiguous match: {len(candidates)} InvenTree objects match '
                f'{mapping.match_inventree_field}={key!r}; refusing to adopt any of them.',
                payload=payload,
            )
        if not candidates:
            return self._create(mapping, synced, payload, digest)
        remote_pk = candidates[0].get('pk')
        if remote_pk is None:
            return self._fail(
                synced,
                f'match candidate for {mapping.match_inventree_field}={key!r} '
                'carries no pk; cannot adopt.',
                payload=payload,
            )
        synced.inventree_pk = remote_pk
        response = self.client.patch(mapping.inventree_endpoint, remote_pk, payload)
        self._mark_synced(synced, digest)
        self._log(synced, SyncActionChoices.ADOPT, payload, response_body=response)
        return 'adopted'

    def _create(self, mapping, synced, payload, digest) -> str:
        """POST a new remote object and link to it."""
        response = self.client.post(mapping.inventree_endpoint, payload)
        remote_pk = response.get('pk') if isinstance(response, dict) else None
        if remote_pk is None:
            return self._fail(
                synced,
                'create response carries no pk; cannot link the remote object.',
                payload=payload,
                response_body=response,
            )
        synced.inventree_pk = remote_pk
        self._mark_synced(synced, digest)
        self._log(synced, SyncActionChoices.CREATE, payload, response_body=response)
        return 'created'

    # --- failure containment ------------------------------------------------------

    def _record_failure(self, synced, exc, payload=None) -> str:
        """Fold one client/transport exception into the link row and log."""
        body = getattr(exc, 'body', None)
        return self._fail(
            synced,
            f'{type(exc).__name__}: {exc}',
            payload=payload,
            response_status=getattr(exc, 'status', None),
            response_body='' if body is None else str(body),
        )

    def _fail(self, synced, message, *, payload=None, response_status=None, response_body='') -> str:
        synced.status = SyncStatusChoices.FAILED
        synced.last_error = self._redact(message)[:MAX_LOG_TEXT]
        synced.save()
        self._log(
            synced, SyncActionChoices.ERROR, payload,
            response_status=response_status, response_body=response_body,
        )
        return 'failed'

    # --- link & log hygiene ---------------------------------------------------------

    def _mark_synced(self, synced, digest) -> None:
        synced.status = SyncStatusChoices.SYNCED
        synced.last_synced_at = timezone.now()
        synced.last_payload_hash = digest
        synced.last_error = ''
        synced.save()

    def _log(self, synced, action, payload, *, response_status=None, response_body='') -> None:
        if response_body is None:
            response_body = ''
        elif not isinstance(response_body, str):
            response_body = json.dumps(response_body, default=str)
        SyncLogEntry.objects.create(
            synced=synced,
            action=action,
            payload=self._redacted_payload(payload),
            response_status=response_status,
            response_body=self._redact(response_body)[:MAX_LOG_TEXT],
        )

    def _redacted_payload(self, payload) -> dict:
        """Payload copy safe for the log: secrets stripped from any string."""
        return json.loads(self._redact(json.dumps(payload or {}, default=str)))

    def _redact(self, text) -> str:
        """Strip the connection token and URL from text bound for the log."""
        text = str(text)
        secrets = [self.connection.api_token, self.connection.url]
        if self.connection.url:
            secrets.append(self.connection.url.rstrip('/'))
        for secret in secrets:
            if secret:
                text = text.replace(secret, '[redacted]')
        return text
