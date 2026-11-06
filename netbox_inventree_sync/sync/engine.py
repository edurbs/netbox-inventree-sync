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

Pointer→related field pairs (a pointer field without a text subfield)
resolve depth-first through pairing mappings: the referenced NetBox object
is published under its pairing BEFORE anything references it, and its
remote pk becomes the field's value. A deleted NetBox object orphans its
link and deactivates — never deletes — the remote object, and
reconciliation authoritatively re-verifies every synced link against the
live InvenTree state.
"""

from __future__ import annotations

import hashlib
import json

import requests
from django.db import transaction
from django.utils import timezone

from netbox_inventree_sync.choices import SyncActionChoices, SyncStatusChoices
from netbox_inventree_sync.models import (
    SyncFieldPair,
    SyncLogEntry,
    SyncMapping,
    SyncedObject,
)
from netbox_inventree_sync.sync.client import InvenTreeClient, InvenTreeClientError
from netbox_inventree_sync.sync.fields import NetboxFieldSpec, get_netbox_fields
from netbox_inventree_sync.sync.payload import extract_value

# Cap for any response/error text persisted on a log entry or the link row.
MAX_LOG_TEXT = 2000

# Pointer→related resolution recursion cap. Mapping validation rejects
# cycles outright; this guards the engine against anything that slips
# through (or a pathological object graph) by failing fast instead of
# recursing to the Python limit.
MAX_POINTER_DEPTH = 10


class PairingResolutionError(Exception):
    """A pointer→related pair cannot be resolved through a pairing mapping.

    Raised when no enabled pairing mapping exists for the pointer target's
    content type, the pointer is unset or unresolvable, the pairing sync
    produced no remote pk, or resolution recursed past
    :data:`MAX_POINTER_DEPTH`. The containing ``sync_object`` call contains
    it on the link row like any other failure — no payload is sent for the
    referencer.
    """


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
        # Endpoint -> whether its writable field specs carry a boolean
        # 'active'; drives the deactivate-on-delete behaviour.
        self._active_field_cache: dict[str, bool] = {}
        # Current pointer→related recursion depth (see MAX_POINTER_DEPTH).
        self._pointer_depth = 0

    # --- public API -----------------------------------------------------------

    def sync_object(self, mapping, netbox_object_id) -> SyncedObject:
        """Push one NetBox object; returns its (claimed or existing) link row."""
        return self._sync(mapping, netbox_object_id)[0]

    def resolve_pointer(self, pair, nb_obj) -> int:
        """Resolve one pointer→related pair to the referenced object's remote pk.

        Finds the enabled pairing mapping on this connection whose content
        type is the pointer target's (``'<app>.<model>'``, from the NetBox
        field spec), syncs the referenced NetBox object under it
        depth-first — the referenced object exists remotely before the
        referencer is pushed — and returns its ``inventree_pk``.

        Raises :class:`PairingResolutionError` on any of: recursion past
        :data:`MAX_POINTER_DEPTH`, no pointer target on the field spec, no
        enabled pairing mapping for the target type, an unset pointer, or
        a pairing sync that produced no remote pk. The containing
        ``sync_object`` call contains the error on the referencer's link
        row; nothing is pushed for it.
        """
        if self._pointer_depth >= MAX_POINTER_DEPTH:
            raise PairingResolutionError(
                f'pointer resolution recursed past {MAX_POINTER_DEPTH} levels '
                f'while resolving {pair.netbox_field!r} on '
                f'{pair.mapping.netbox_content_type}; cycle?'
            )
        spec = self._netbox_specs(pair.mapping).get(pair.netbox_field)
        pointer_model = spec.pointer_model if spec is not None else None
        if not pointer_model:
            raise PairingResolutionError(
                f'{pair.netbox_field!r} on {pair.mapping.netbox_content_type} '
                'carries no pointer target; cannot resolve a pairing mapping.',
            )
        app_label, _, model_name = pointer_model.rpartition('.')
        if not app_label or not model_name:
            raise PairingResolutionError(
                f'malformed pointer target {pointer_model!r} on '
                f'{pair.netbox_field!r}; cannot resolve a pairing mapping.',
            )
        pairing = SyncMapping.objects.filter(
            connection=self.connection,
            enabled=True,
            netbox_content_type__app_label=app_label,
            netbox_content_type__model=model_name,
        ).first()
        if pairing is None:
            raise PairingResolutionError(
                f'no enabled pairing mapping for {pointer_model} on '
                f'{self.connection}; cannot resolve {pair.netbox_field!r}.',
            )
        target = getattr(nb_obj, pair.netbox_field, None)
        if target is None:
            raise PairingResolutionError(
                f'{pair.netbox_field!r} is not set on '
                f'{pair.mapping.netbox_content_type} #{nb_obj.pk}; '
                'nothing to resolve.',
            )
        self._pointer_depth += 1
        try:
            target_synced = self.sync_object(pairing, target.pk)
        finally:
            self._pointer_depth -= 1
        if target_synced.inventree_pk is None:
            raise PairingResolutionError(
                f'pairing sync of {pointer_model} #{target.pk} produced no '
                f'InvenTree pk ({target_synced.last_error or target_synced.status}); '
                f'cannot resolve {pair.netbox_field!r}.',
            )
        return target_synced.inventree_pk

    def handle_delete(self, mapping, netbox_object_id) -> SyncedObject:
        """Apply delete semantics for one gone NetBox object; never raises.

        The link row is marked ``orphaned`` and the remote object is
        DEACTIVATED, never deleted: when the endpoint's writable field
        specs carry a boolean ``active`` it is PATCHed to ``false``,
        otherwise the transition is local-only with the reason noted on
        the ``deactivate`` log entry. ``inventree_pk`` is kept so the
        remote object stays locatable. Client/transport failures are
        contained like everywhere else: the link still goes ``orphaned``
        and the failure lands in an ``error`` log entry.
        """
        with transaction.atomic():
            synced = self._claim(mapping, netbox_object_id)
            synced.status = SyncStatusChoices.ORPHANED
            synced.save()
            try:
                self._deactivate_remote(mapping, synced)
            except (InvenTreeClientError, requests.RequestException) as exc:
                self._log(
                    synced, SyncActionChoices.ERROR, {'active': False},
                    response_status=getattr(exc, 'status', None),
                    response_body=f'{type(exc).__name__}: {exc}',
                )
        synced.refresh_from_db()
        return synced

    def verify_remote(self, mapping, synced) -> dict:
        """Authoritatively verify one synced link against the live remote.

        GETs the linked InvenTree object and compares ONLY the mapped
        fields (the freshly built payload) against it; any difference is
        repaired by PATCHing the expected values back. A missing remote
        (HTTP 404) counts as drift and re-creates the object from the
        payload. Returns ``{'drifted': bool}``; failures to verify or
        repair are contained on the link row like any other error.
        """
        if synced.inventree_pk is None:
            return {'drifted': False}
        netbox_obj = self._fetch_netbox_object(mapping, synced.netbox_object_id)
        if netbox_obj is None:
            # NetBox object gone: nothing to verify against — the delete
            # pass (handle_delete) owns that transition.
            return {'drifted': False}
        payload = self._build_payload(mapping, netbox_obj)
        digest = canonical_hash(payload)
        try:
            remote = self.client.get(mapping.inventree_endpoint, synced.inventree_pk)
        except (InvenTreeClientError, requests.RequestException) as exc:
            if isinstance(exc, InvenTreeClientError) and exc.status == 404:
                self._recreate(mapping, synced, payload, digest)
                return {'drifted': True}
            self._record_failure(synced, exc, payload)
            return {'drifted': False}
        drift = {
            field: value for field, value in payload.items()
            if not self._remote_field_matches(remote.get(field), value)
        }
        if not drift:
            return {'drifted': False}
        try:
            response = self.client.patch(
                mapping.inventree_endpoint, synced.inventree_pk, payload,
            )
        except (InvenTreeClientError, requests.RequestException) as exc:
            self._record_failure(synced, exc, payload)
            return {'drifted': True}
        self._mark_synced(synced, digest)
        self._log(synced, SyncActionChoices.UPDATE, payload, response_body=response)
        return {'drifted': True}

    def reconcile_mapping(self, mapping) -> dict:
        """Sync every object in ``mapping``'s scope; return outcome counts.

        Three passes, in order. (1) Authoritative verification: every
        ``synced`` link is GET-checked against a freshly built payload and
        drift is repaired — missing remotes are re-created. (2) Sync:
        pending/failed links are retried, orphaned links whose NetBox
        object returned are revived, and every NetBox object of the mapped
        content type without a link is published through the mapping's
        match/adopt policy. (3) Delete semantics: links whose NetBox
        object is gone are orphaned and remotely deactivated. Rows already
        ``orphaned`` keep their status unless their NetBox object returns.
        """
        stats = dict.fromkeys(OUTCOMES, 0)
        model = mapping.netbox_content_type.model_class()
        # Snapshot statuses first so each link is driven by exactly one
        # pass: a link that turns failed during verification is not also
        # retried below, and an orphaned row is never re-failed.
        snapshot = {
            row.netbox_object_id: row.status
            for row in mapping.synced_objects.all()
        }

        # Pass 1 — authoritative remote verification of synced links.
        for row in mapping.synced_objects.filter(status=SyncStatusChoices.SYNCED):
            if not self._netbox_object_exists(model, row.netbox_object_id):
                continue  # gone: the delete pass owns this link
            result = self.verify_remote(mapping, row)
            row.refresh_from_db()
            if row.status == SyncStatusChoices.FAILED:
                stats['failed'] += 1
            else:
                stats['updated' if result.get('drifted') else 'unchanged'] += 1

        # Pass 2 — (re)sync: retry pending/failed links, revive orphaned
        # links whose NetBox object returned, publish unlinked objects.
        sync_ids = {
            netbox_object_id for netbox_object_id, status in snapshot.items()
            if status in (SyncStatusChoices.PENDING, SyncStatusChoices.FAILED)
            or (status == SyncStatusChoices.ORPHANED
                and self._netbox_object_exists(model, netbox_object_id))
        }
        if model is not None:
            sync_ids.update(
                model.objects.exclude(pk__in=snapshot).values_list('pk', flat=True)
            )
        for netbox_object_id in sorted(sync_ids):
            stats[self._sync(mapping, netbox_object_id)[1]] += 1

        # Pass 3 — delete semantics for links whose NetBox object is gone;
        # already-orphaned rows keep their status and remote state.
        for netbox_object_id, status in snapshot.items():
            if status == SyncStatusChoices.ORPHANED:
                continue
            if not self._netbox_object_exists(model, netbox_object_id):
                self.handle_delete(mapping, netbox_object_id)
                stats['orphaned'] += 1
        return stats

    def deactivate(self, synced) -> None:
        """Mark ``synced`` orphaned (its NetBox object is gone) and log it.

        Local state transition only — :meth:`handle_delete` is the entry
        point that adds the remote deactivate. ``inventree_pk`` is kept so
        the remote object stays locatable.
        """
        with transaction.atomic():
            locked = SyncedObject.objects.select_for_update().get(pk=synced.pk)
            locked.status = SyncStatusChoices.ORPHANED
            locked.save()
            self._log(locked, SyncActionChoices.DEACTIVATE, {})
        synced.refresh_from_db()

    def _deactivate_remote(self, mapping, synced) -> None:
        """PATCH ``{"active": false}`` when possible; otherwise log why not.

        Raises client/transport errors to the caller (``handle_delete``
        contains them); never issues a DELETE — deactivation keeps the
        remote object and its history intact.
        """
        if synced.inventree_pk is None:
            self._log(
                synced, SyncActionChoices.DEACTIVATE, {},
                response_body='no linked remote object; deactivated locally only',
            )
            return
        if not self._endpoint_has_active(mapping.inventree_endpoint):
            self._log(
                synced, SyncActionChoices.DEACTIVATE, {},
                response_body=(
                    f'{mapping.inventree_endpoint} has no writable boolean '
                    '"active" field; deactivated locally only'
                ),
            )
            return
        response = self.client.patch(
            mapping.inventree_endpoint, synced.inventree_pk, {'active': False},
        )
        self._log(
            synced, SyncActionChoices.DEACTIVATE, {'active': False},
            response_body=response,
        )

    def _endpoint_has_active(self, endpoint) -> bool:
        """True when the endpoint's writable field specs carry boolean 'active'.

        Cached per endpoint for the engine's lifetime (one job); the client
        caches the underlying OPTIONS call on top of that.
        """
        if endpoint not in self._active_field_cache:
            specs = self.client.get_field_specs(endpoint)
            self._active_field_cache[endpoint] = any(
                spec.name == 'active' and spec.type == 'boolean' for spec in specs
            )
        return self._active_field_cache[endpoint]

    # --- claim & drive ----------------------------------------------------------

    def _sync(self, mapping, netbox_object_id) -> tuple[SyncedObject, str]:
        """Claim, then push one object; never raises client/transport errors.

        The row lock taken by :meth:`_claim` is held for the whole block —
        including the HTTP calls — so a second job arriving mid-push blocks
        on the lock and afterwards takes the update/no-op path. Resolution
        failures (no usable pairing mapping) are contained the same way:
        the payload build raises before anything is sent.
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
            except (
                InvenTreeClientError,
                requests.RequestException,
                PairingResolutionError,
            ) as exc:
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
        """The full InvenTree payload for one object, built before any send.

        Pointer→related pairs (a pointer field without a text subfield)
        resolve depth-first through their pairing mapping — the referenced
        NetBox object is synced under the pairing first, and its remote pk
        becomes the field's value. An unset (optional) pointer maps to an
        explicit ``None``.
        """
        specs = self._netbox_specs(mapping)
        payload = {}
        for pair in mapping.field_pairs.all():
            spec = specs.get(pair.netbox_field)
            if spec is not None and spec.type == 'pointer' and not pair.netbox_subfield:
                target = getattr(netbox_obj, pair.netbox_field, None)
                payload[pair.inventree_field] = (
                    None if target is None
                    else self.resolve_pointer(pair, netbox_obj)
                )
                continue
            payload[pair.inventree_field] = extract_value(netbox_obj, pair)
        return payload

    def _netbox_specs(self, mapping) -> dict[str, NetboxFieldSpec]:
        ct_id = mapping.netbox_content_type_id
        if ct_id not in self._netbox_spec_cache:
            self._netbox_spec_cache[ct_id] = {
                spec.name: spec for spec in get_netbox_fields(mapping.netbox_content_type)
            }
        return self._netbox_spec_cache[ct_id]

    @staticmethod
    def _netbox_object_exists(model, netbox_object_id) -> bool:
        """False when the model is stale or the object was deleted."""
        return model is not None and model.objects.filter(pk=netbox_object_id).exists()

    @staticmethod
    def _remote_field_matches(remote_value, expected) -> bool:
        """Compare one remote field value against the freshly built one.

        Empty-string vs ``None`` counts as a match: InvenTree frequently
        round-trips an empty string as ``None``, and treating that as drift
        would PATCH identical payloads on every verification pass.
        """
        if remote_value is None and expected == '':
            return True
        if remote_value == '' and expected is None:
            return True
        return remote_value == expected

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
        # A pointer match field has no scalar key to match on (and is never
        # resolved through a pairing here): treat it as a blank key rather
        # than using the related object itself as the lookup value.
        match_spec = self._netbox_specs(mapping).get(mapping.match_netbox_field)
        if match_spec is not None and match_spec.type == 'pointer':
            return self._fail(
                synced,
                f'blank match key: {mapping.match_netbox_field!r} on '
                f'{mapping.netbox_content_type} is a pointer field; map it '
                'as text with a subfield to use it for matching.',
                payload=payload,
            )
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
        # Kept even when the follow-up PATCH fails (_fail saves the row):
        # a retry then takes the update path against the adopted object
        # instead of re-adopting or re-creating — deliberate mid-flight pk
        # persistence.
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
        # Persisted even if a later step fails (_fail saves the row too):
        # the retry takes the update path, never a duplicate create.
        synced.inventree_pk = remote_pk
        self._mark_synced(synced, digest)
        self._log(synced, SyncActionChoices.CREATE, payload, response_body=response)
        return 'created'

    def _recreate(self, mapping, synced, payload, digest) -> None:
        """Heal a link whose remote object vanished: publish a replacement.

        The 404 is logged for the audit trail, the stale pk is dropped and
        :meth:`_create` stores the replacement pk (and its own ``create``
        log entry). A failing re-create is contained on the link row.
        """
        self._log(
            synced, SyncActionChoices.ERROR, payload,
            response_status=404,
            response_body='remote object missing (HTTP 404); re-creating',
        )
        synced.inventree_pk = None
        synced.save()
        self._create(mapping, synced, payload, digest)

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
