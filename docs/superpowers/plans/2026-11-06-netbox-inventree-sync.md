# NetBox ↔ InvenTree Sync Plugin — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A NetBox plugin that pushes user-configured field mappings of NetBox objects to InvenTree via its REST API, event-driven with periodic reconciliation, plus a local NetBox + InvenTree stack and E2E test suite.

**Architecture:** Plugin `netbox_inventree_sync` developed against a local NetBox dev install (tutorial workflow). Mapping config (NetBox content type ↔ InvenTree endpoint + typed field pairs) lives in plugin models; a sync engine pushes via a thin InvenTree REST client, guarded by a link table (never-guess adoption, race-safe claims). Django signals enqueue RQ jobs; per-mapping recurring reconcile jobs repair drift authoritatively (GET + compare + PATCH).

**Tech Stack:** Python 3.12, Django/NetBox v4.7.4 plugin API (NetBoxModel, JobRunner, django-rq), requests, InvenTree 1.7.0 REST API (token auth, OPTIONS metadata), docker compose (Postgres 17 + Redis 7 per app), pytest for E2E.

**Spec:** `docs/superpowers/specs/2026-11-06-netbox-inventree-sync-design.md` — read it; this plan argues from it.

## Global Constraints

- NetBox pinned at tag `v4.7.4`, cloned to `./netbox` (not committed; in `.gitignore`), plugin installed editable (`pip install -e .`) into `./netbox/venv`.
- NetBox dev config: `DEBUG = True`, `DEVELOPER = True`, `PLUGINS = ['netbox_inventree_sync']`.
- InvenTree pinned at docker image `inventree/inventree:1.7.0`; only its REST API is used (token auth, `Authorization: Token <token>`); never its DB.
- Plugin package/Django app/PluginConfig name: `netbox_inventree_sync`; nav menu "InvenTree Sync".
- Plugin layout follows the NetBox plugin tutorial (`models.py`, `tables.py`, `forms.py`, `views.py`, `urls.py`, `navigation.py`, `filtersets.py`, `search.py`, `template_content.py`, `signals.py`, `jobs.py`, `api/`, `graphql/`, `templates/netbox_inventree_sync/`, `migrations/`); only documented plugin APIs.
- Ports: NetBox `localhost:8000`, InvenTree `localhost:1337`, netbox-db `127.0.0.1:15432`, netbox-redis `127.0.0.1:16379` (InvenTree services internal only).
- Unit tests run inside the NetBox environment: `cd netbox && ./venv/bin/python manage.py test netbox_inventree_sync` (requires compose services up).
- E2E tests: `cd netbox && ./venv/bin/python -m pytest ../tests/e2e -v` against the live pair; env vars `NETBOX_URL`, `NETBOX_TOKEN`, `INVENTREE_URL`, `INVENTREE_TOKEN` (conftest defaults match `scripts/seed_netbox.py` and `scripts/seed_inventree.py`).
- Dev/test credentials fixed: netbox admin `admin`/`admin` (token created by seed); inventree admin `admin`/`admin` (token created by seed).
- Never DELETE on the InvenTree side; deactivate instead. Never adopt/create when a match is ambiguous or the match key is blank — flag only.
- Scheduled reconcile is realized as per-mapping recurring jobs via `JobRunner.enqueue_once(instance=mapping, interval=...)` (NetBox `@system_job` intervals are fixed at registration, so the spec's "configurable interval" maps to this equivalent mechanism).

## Review Focus

1. **InvenTree unreachable mid-push** (timeout/conn refused) → job/object ends `failed` with the error stored, nothing raised into NetBox's request path; E2E 7 proves reconcile heals it. → Task 8 unit test + Task 16 E2E.
2. **InvenTree 4xx validation** (e.g. NetBox 200-char text into InvenTree `string` `max_length` 100) → `failed` with response body captured in the log; no silent truncation, no crash. → Task 8 unit test.
3. **Pairing mapping disabled/missing at push time** for a pointer→related pair → clear error on the object, payload not sent at all (no partial POST). → Task 9 unit test.
4. **Duplicate creates under concurrent edits** (two jobs, same object, both see "no link") → exactly one InvenTree object; second job takes the update/no-op path. → Task 8 unit test (claim + row lock).
5. **Adoption ambiguity** (match returns 2+ candidates, or NetBox match-key field blank) → object flagged `failed` with "ambiguous match"/"blank match key" error; nothing adopted or created. → Task 8 unit test. (Client-side pagination correctness beyond one page is pinned in Task 4's tests.)

---

### Task 1: NetBox dev environment + plugin scaffold

**Files:**
- Create: `docker-compose.yml` (netbox-db, netbox-redis only), `.gitignore`, `.env`
- Create: `config/configuration.py.tmpl`, `scripts/setup_netbox.sh`, `scripts/seed_netbox.py`, `scripts/start.sh`
- Create: `netbox_inventree_sync/__init__.py` (PluginConfig), `pyproject.toml`, `README.md` (stub)

**Interfaces:**
- Produces: running NetBox at `localhost:8000` with the plugin loaded; `PluginConfig(name='netbox_inventree_sync', verbose_name='InvenTree Sync')`; script entry points later tasks call (`scripts/setup_netbox.sh`, `scripts/seed_netbox.py` writes admin + token `admin`).

- [ ] **Step 1: Verify Python 3.12+ and docker available**

Run: `python3.12 --version && docker compose version`
Expected: Python 3.12.x; compose v2. If missing, stop and report — do not improvise installs.

- [ ] **Step 2: Write compose file, config template, scripts, plugin scaffold**

`docker-compose.yml`: services `netbox-db` (postgres:17-alpine, env from `.env`, port `127.0.0.1:15432:5432`), `netbox-redis` (redis:7-alpine, port `127.0.0.1:16379:6379`). `.env` holds `POSTGRES_PASSWORD=netbox` etc. `.gitignore`: `netbox/`, `.venv/`, `*.pyc`, `data/`, `.env`. `scripts/setup_netbox.sh`: clone netbox at tag `v4.7.4` into `./netbox`, create `netbox/venv` with `python3.12`, `pip install -r requirements.txt`, copy `config/configuration.py.tmpl` to `netbox/netbox/configuration.py`, `manage.py migrate`, run `scripts/seed_netbox.py`, `pip install -e ..` (the plugin), `manage.py migrate` again (plugin tables). `PluginConfig` per tutorial step 1 with `menu` left for Task 12. `pyproject.toml` per tutorial (`[project] name = "netbox-inventree-sync"`, requires-python `>=3.12`, dependencies `requests`).

- [ ] **Step 3: Run setup and verify NetBox serves with the plugin**

Run: `./scripts/setup_netbox.sh && ./scripts/start.sh` (start.sh: `docker compose up -d`, then `runserver 0.0.0.0:8000` and `rqworker high default low` in background with logs to `/tmp/opencode/`)
Then: `curl -s -H "Authorization: Token $(grep NETBOX_TOKEN .env | cut -d= -f2)" http://localhost:8000/api/plugins/status/ | grep -o netbox_inventree_sync`
Expected: plugin name present in installed plugins (or `/api/status/` plugins section on 4.7 — check whichever endpoint lists plugins).

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml .gitignore .env.example config scripts netbox_inventree_sync pyproject.toml README.md
git commit -m "feat: netbox dev environment and plugin scaffold"
```

(Commit `.env.example`; `.env` stays ignored but seed writes tokens there for later tasks.)

### Task 2: InvenTree stack + seed + smoke

**Files:**
- Modify: `docker-compose.yml` (add inventree-db, inventree-redis, inventree-web, inventree-worker)
- Create: `scripts/setup_inventree.sh`, `scripts/seed_inventree.py`, `scripts/smoke_inventree.py`

**Interfaces:**
- Produces: InvenTree at `http://localhost:1337`, admin `admin`/`admin`, API token in `.env` as `INVENTREE_TOKEN`; verified `OPTIONS /api/part/` field metadata.
- Consumes: `.env` convention from Task 1.

- [ ] **Step 1: Extend compose with InvenTree 1.7.0**

Services per InvenTree docker docs: `inventree-web` and `inventree-worker` on image `inventree/inventree:1.7.0`, `inventree-db` (postgres:17-alpine, internal), `inventree-redis` (redis:7-alpine, internal), shared volume `inventree-data`; env `INVENTREE_DB_*`, `INVENTREE_WEB_URL=http://localhost:1337`, `INVENTREE_DEBUG=False`; port `127.0.0.1:1337:8000`.

- [ ] **Step 2: Write setup + seed + smoke scripts**

`setup_inventree.sh`: `docker compose up -d inventree-db inventree-redis`, wait healthy, `docker compose run --rm inventree-web invoke update` (migrations/static), then `seed_inventree.py` (executed inside container: `docker compose exec -T inventree-web inventree-shell < scripts/seed_inventree.py`) creating superuser `admin`/`admin` and an `ApiToken` (all scopes), printing the key; script stores it in `../.env` as `INVENTREE_TOKEN`. `smoke_inventree.py` (host, plain requests): assert `GET /api/part/` with token → 200, and `OPTIONS /api/part/` JSON contains `name` (string), `IPN`, `active` (boolean), `category` (related field with `model` metadata); print the parsed field count.

- [ ] **Step 3: Run and verify**

Run: `./scripts/setup_inventree.sh && python3 scripts/smoke_inventree.py` (or netbox venv python if requests missing on host)
Expected: smoke exits 0 printing ≥ 20 fields for `/api/part/`; `.env` contains `INVENTREE_TOKEN=...`.

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml scripts/smoke_inventree.py scripts/setup_inventree.sh scripts/seed_inventree.py
git commit -m "feat: inventree 1.7.0 test stack with seeded api token"
```

### Task 3: Plugin models + migrations

**Files:**
- Create: `netbox_inventree_sync/models.py`, `netbox_inventree_sync/choices.py`, `netbox_inventree_sync/migrations/0001_initial.py`
- Test: `netbox_inventree_sync/tests/test_models.py`

**Interfaces:**
- Produces (exact fields; later tasks rely on these):
  - `InvenTreeInstance(NetBoxModel)`: `name` (CharField 100), `url` (URLField), `api_token` (CharField 255), `enabled` (Boolean default True); `__str__` = name; `get_absolute_url`.
  - `SyncMapping(NetBoxModel)`: `name` (100), `connection` FK `InvenTreeInstance` PROTECT, `netbox_content_type` FK `contenttypes.ContentType` PROTECT, `inventree_endpoint` (Char 200, e.g. `/api/part/`), `match_netbox_field` (Char 100, blank), `match_inventree_field` (Char 100, blank), `reconcile_interval_minutes` (PositiveSmallInteger default 60), `enabled` (Boolean default False), `description` (TextField blank). `UniqueConstraint(connection, netbox_content_type, inventree_endpoint)`.
  - `SyncFieldPair(NetBoxModel)`: `mapping` FK PROTECT related_name='field_pairs', `netbox_field` (Char 100), `netbox_subfield` (Char 100, blank — pointer attribute for as-text), `inventree_field` (Char 100), `transform` (Char choices `none|iso_date|str`, default `none`).
  - `SyncedObject(NetBoxModel)`: `mapping` FK PROTECT related_name='synced_objects', `netbox_object_id` (PositiveBigIntegerField), `inventree_pk` (PositiveIntegerField null), `status` (Char choices `pending|synced|failed|orphaned`, default `pending`), `last_synced_at` (DateTimeField null), `last_payload_hash` (CharField 64 blank), `last_error` (TextField blank). `UniqueConstraint(mapping, netbox_object_id)`.
  - `SyncLogEntry(NetBoxModel)`: `synced` FK `SyncedObject` SET_NULL null related_name='log_entries', `action` (Char choices `create|update|deactivate|adopt|error`), `payload` (JSONField default dict), `response_status` (PositiveSmallIntegerField null), `response_body` (TextField blank), auto timestamps.
- Produces: `choices.py` with `SyncStatusChoices`, `SyncActionChoices`, `TransformChoices` (TextChoices classes; `.status_display` via NetBoxModel conventions).

- [ ] **Step 1: Write failing tests**

`test_models.py`: `test_synced_object_unique_per_mapping` (second row same (mapping, netbox_object_id) raises IntegrityError), `test_mapping_multi_endpoint_allowed` (same connection+CT, two endpoints → both valid), `test_str_methods`, `test_status_choices_values` (exactly `pending|synced|failed|orphaned`).

- [ ] **Step 2: Run to verify failure**

Run: `cd netbox && ./venv/bin/python manage.py test netbox_inventree_sync.tests.test_models -v 1`
Expected: FAIL (no module `models` content yet / import error).

- [ ] **Step 3: Implement models + generate migration**

Subclass `NetBoxModel` (tutorial step 2). Generate with `DEVELOPER=True`: `./venv/bin/python manage.py makemigrations netbox_inventree_sync --no-header` from `netbox/` dir.

- [ ] **Step 4: Run tests, verify pass**

Run: same as Step 2. Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add netbox_inventree_sync/models.py netbox_inventree_sync/choices.py netbox_inventree_sync/migrations/ netbox_inventree_sync/tests/test_models.py
git commit -m "feat: plugin data model (connection, mapping, pairs, link table, log)"
```

### Task 4: InvenTree API client

**Files:**
- Create: `netbox_inventree_sync/sync/__init__.py`, `netbox_inventree_sync/sync/client.py`
- Test: `netbox_inventree_sync/tests/test_client.py`

**Interfaces:**
- Produces:
  - `InventreeFieldSpec` dataclass: `name: str`, `type: str` (`string|boolean|integer|float|related`), `required: bool`, `max_length: int | None`, `related_model: str | None`.
  - `class InvenTreeClient(url: str, token: str, timeout: tuple = (10, 30))` raising `InvenTreeClientError(message, status=None, body=None)`:
    - `test_connection() -> dict` (GET `/api/`; returns `{"user": ..., "version": ...}` or raises)
    - `get_field_specs(endpoint: str) -> list[InventreeFieldSpec]` (OPTIONS; parse `actions.POST`/`actions.PUT` merged, prefer PUT when present; skip `read_only` fields; related fields read `model` attr; cache per (url, endpoint) with 5-min TTL via `functools`/dict)
    - `list(endpoint: str, params: dict | None = None) -> list[dict]` — follows DRF pagination (`results`/`next`) to the end
    - `get(endpoint, pk) -> dict`
    - `post(endpoint, data) -> dict`
    - `patch(endpoint, pk, data) -> dict`
    - `match(endpoint, field, value) -> list[dict]` — try `params={field: value}`; if the server rejects the filter (400), fall back to `search=value` and client-side exact case-sensitive equality on `field`
- Consumes: none (pure transport; no NetBox models).

- [ ] **Step 1: Write failing tests (mocked transport with `responses`)**

Add `responses` + `pytest` to `pyproject.toml` dev extras and install into netbox venv. Tests: `test_field_specs_parse` (OPTIONS fixture of `/api/part/` → `name` string required, `IPN` string max 100, `active` boolean, `category` related `related_model='partcategory'`, read-only `creation_date` absent), `test_list_follows_pagination` (two pages, 3 items total), `test_match_uses_filter_param`, `test_match_falls_back_to_search_and_filters_exactly` (2 search hits, 1 exact match returned), `test_error_raises_client_error_with_body` (400 → `InvenTreeClientError.status == 400`, body preserved), `test_connection_error_raises_client_error` (`responses` connection error → raises, no escape).

- [ ] **Step 2: Run to verify failure** — `cd netbox && ./venv/bin/python manage.py test netbox_inventree_sync.tests.test_client -v 1`; expected FAIL (module missing).

- [ ] **Step 3: Implement `client.py`**

`requests.Session` with `Authorization: Token <token>` header; raise-for-status wrapped into `InvenTreeClientError`; pagination loop caps at 100 pages (guard).

- [ ] **Step 4: Run tests, verify pass** — same command; expected PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add netbox_inventree_sync/sync/ netbox_inventree_sync/tests/test_client.py pyproject.toml
git commit -m "feat: inventree rest client with options introspection and pagination"
```

### Task 5: NetBox field introspection

**Files:**
- Create: `netbox_inventree_sync/sync/fields.py`
- Test: `netbox_inventree_sync/tests/test_fields.py`

**Interfaces:**
- Produces:
  - `NetboxFieldSpec` dataclass: `name: str`, `label: str`, `type: str` (`text|boolean|integer|float|date|pointer`), `pointer_model: str | None` (e.g. `'dcim.manufacturer'`), `custom: bool = False`, `subfield_options: list[str] = []` (text attributes of the pointer target, e.g. `['name']`).
  - `get_netbox_fields(content_type: ContentType) -> list[NetboxFieldSpec]` — concrete model fields (exclude auto pk/created fields and reverse relations), custom fields (`CustomField.objects.filter(assigned_models=...)` or the object-class filter; type map: text/longtext→text, integer→integer, boolean→boolean, date→date, url→text, select→text, object→pointer with `pointer_model`), and `tags` → text (comma-joined values) when the model has tags. FK/O2O fields → `pointer` with `pointer_model='<app>.<model>'` and `subfield_options` = the target's non-nullable CharField names (cap 5, sorted, `name` first if present).
- Consumes: Task 3 models not needed; pure Django/NetBox metadata.

- [ ] **Step 1: Write failing tests**

Using real NetBox models (no fixtures needed beyond ContentType lookups): `test_inventoryitem_fields` (`dcim.inventoryitem` specs contain `name` text, `serial` text, `discovered` boolean, `manufacturer` pointer `pointer_model='dcim.manufacturer'`), `test_manufacturer_pointer_subfields` (`manufacturer` spec `subfield_options` starts with `name`), `test_custom_fields_included` (create `CustomField` type text assigned to dcim.inventoryitem → appears `custom=True`, type text; cleanup in test), `test_tags_text_spec` (`tags` present, type text).

- [ ] **Step 2: Run to verify failure** — `cd netbox && ./venv/bin/python manage.py test netbox_inventree_sync.tests.test_fields -v 1`; FAIL.

- [ ] **Step 3: Implement `fields.py`**

Map Django field classes: `CharField/TextField/URLField/EmailField/SlugField → text`, `BooleanField → boolean`, `IntegerField family → integer`, `DecimalField/FloatField → float`, `DateField → date`, `ForeignKey/OneToOneField → pointer`. Exclude `ManyToManyField`, `GenericForeignKey`.

- [ ] **Step 4: Run tests, verify pass** — PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add netbox_inventree_sync/sync/fields.py netbox_inventree_sync/tests/test_fields.py
git commit -m "feat: netbox field introspection with pointer subfields and custom fields"
```

### Task 6: Compatibility matrix

**Files:**
- Create: `netbox_inventree_sync/sync/compatibility.py`
- Test: `netbox_inventree_sync/tests/test_compatibility.py`

**Interfaces:**
- Produces:
  - `compatible(nb: NetboxFieldSpec, inv: InventreeFieldSpec, available_pairings: set[str]) -> bool` — implements the spec's matrix exactly; pointer→related only when `nb.pointer_model in available_pairings`; pointer→string allowed (as text, requires `subfield_options` non-empty).
  - `transform_for(nb_type: str, inv_type: str) -> str` — `'iso_date'` for date→string, `'str'` for pointer→string, else `'none'`.

- [ ] **Step 1: Write failing tests**

Parametrized over the full spec matrix rows: text→string ✓, boolean→string ✗, integer→integer ✓, decimal→float ✓, date→string ✓ (transform `iso_date`), pointer→string ✓ when `subfield_options=['name']` (transform `str`), pointer→related ✓ iff pairing present, text→related ✗, text→boolean ✗. Plus: pointer with empty `subfield_options` → string ✗.

- [ ] **Step 2: Run to verify failure** — FAIL (module missing).

- [ ] **Step 3: Implement `compatibility.py`** (pure functions, dict lookup).

- [ ] **Step 4: Run tests, verify pass** — PASS.

- [ ] **Step 5: Commit**

```bash
git add netbox_inventree_sync/sync/compatibility.py netbox_inventree_sync/tests/test_compatibility.py
git commit -m "feat: field type compatibility matrix"
```

### Task 7: Mapping validation

**Files:**
- Create: `netbox_inventree_sync/sync/validation.py`
- Test: `netbox_inventree_sync/tests/test_validation.py`

**Interfaces:**
- Consumes: Task 5 `get_netbox_fields`, Task 4 `InvenTreeClient.get_field_specs`, Task 6 `compatible`/`transform_for`, Task 3 models.
- Produces: `validate_mapping(mapping: SyncMapping, pairs: list[SyncFieldPair]) -> list[SyncFieldPair]` (returns pairs with `transform` auto-set) raising `django.core.exceptions.ValidationError` with coded messages:
  - `incompatible` (pair fails `compatible`; `available_pairings` = content types of the connection's *enabled* mappings by `str(ct)`),
  - `duplicate_target` (two pairs → same `inventree_field`),
  - `unknown_field` (field name not in specs),
  - `pairing_missing` (pointer→related whose pairing mapping is absent/disabled),
  - `cycle` (`validate_mapping_graph(mappings: Iterable[SyncMapping])` walks dependency edges mapping→pairing-mapping within a connection; cycle → `ValidationError`).
  Also sets `netbox_subfield` required for pointer→string pairs (`subfield_required`).

- [ ] **Step 1: Write failing tests**

Create in test: connection + three mappings (inventoryitem→/api/part/, manufacturer→/api/company/, role→/api/part/category/) with client field specs stubbed via a fake `get_field_specs` (monkeypatched module attr). Tests: compatible valid set returns pairs with transforms set (`part_id`→`IPN` none; `manufacturer.name`→`keywords` `str`); boolean→string raises `incompatible`; two pairs→`description` raise `duplicate_target`; role mapping disabled → part mapping's role→category pair raises `pairing_missing`; self-referencing test for `cycle` (two mappings whose pointer pairs reference each other's content types, enabled) raises; pointer→string without subfield raises `subfield_required`.

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement `validation.py`**

Graph for cycles: nodes = enabled mappings of a connection; edge M→P when M has a pointer→related pair whose pairing is P; DFS cycle detect.

- [ ] **Step 4: Run tests, verify pass** — PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add netbox_inventree_sync/sync/validation.py netbox_inventree_sync/tests/test_validation.py
git commit -m "feat: mapping validation (compatibility, duplicates, pairings, cycles)"
```

### Task 8: Sync engine core

**Files:**
- Create: `netbox_inventree_sync/sync/payload.py`, `netbox_inventree_sync/sync/engine.py`
- Test: `netbox_inventree_sync/tests/test_engine.py`

**Interfaces:**
- Consumes: Task 4 client (constructor-injected: `SyncEngine(connection, client=None)` builds `InvenTreeClient` when None), Task 3 models, Task 7 validation (not called here).
- Produces:
  - `extract_value(obj, pair) -> str | int | bool | float | None` in `payload.py`: plain fields, custom fields (`obj.custom_field_data[name]`), `tags` (comma-joined tag slugs), pointer-as-text (`getattr(obj, nb_field).<subfield>` via `netbox_subfield`), transforms `iso_date` (`value.isoformat()`) and `str`.
  - `class SyncEngine`: `sync_object(mapping, netbox_object_id) -> SyncedObject`; `reconcile_mapping(mapping) -> dict` (`{created, updated, adopted, failed, orphaned, unchanged}`); `deactivate(synced) -> None`.
  - Internal contract pinned by tests: claim via `SyncedObject.objects.select_for_update()` inside `transaction.atomic()`; `get_or_create` before any HTTP call; payload built fully before sending; hash = sha256 of canonical json (sorted keys).
- [ ] **Step 1: Write failing tests** (fake client class recording calls, no network)

`test_create_when_no_link_and_always_create` (no match key; POST once; link `synced`; log `create`; hash stored); `test_update_patches_diff_only` (existing link; payload differs from `last_payload_hash` → PATCH changed keys only; identical hash → no HTTP call); `test_adopt_single_exact_match` (match key pair `part_id`→`IPN`; fake client match returns exactly one → adopt, log `adopt`, PATCH drift); `test_ambiguous_match_flags_never_adopts` (match returns 2 → status failed, `last_error` contains "ambiguous match", zero POST); `test_blank_match_key_flags` (object's match field blank → failed "blank match key", zero POST); `test_client_error_marks_failed_and_logs` (client.post raises `InvenTreeClientError(status=400, body=...)` → failed; log entry `error` with `response_status=400` and body containing the server message); `test_connection_error_marks_failed` (transport error variant); `test_concurrent_claims_create_once` (claim row pre-created in `pending` by "other job", second call with empty POST history → only PATCH/no-op path; assert fake client `.posts` length ≤ 1); `test_token_redacted_in_logs` (URL/token never appears in `response_body`/`payload` strings).
- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement `engine.py` + `payload.py`**

Flow per spec: claim (atomic, select_for_update) → build payload (extract values; pointer→related pairs raise `PairingDisabled` here only if pairing mapping missing — those pairs are stored but skipped in this task's plain tests) → hash compare → link? PATCH : (match key? adopt-or-create : create) → update link + log. All `InvenTreeClientError`/`requests.RequestException` caught per object into `status=failed`, `last_error`, log `error` — never propagate.

- [ ] **Step 4: Run tests, verify pass** — PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add netbox_inventree_sync/sync/engine.py netbox_inventree_sync/sync/payload.py netbox_inventree_sync/tests/test_engine.py
git commit -m "feat: sync engine core with claims, adoption policy and error capture"
```

### Task 9: Pointer resolution + delete semantics

**Files:**
- Modify: `netbox_inventree_sync/sync/engine.py`
- Test: `netbox_inventree_sync/tests/test_pointer_resolution.py`

**Interfaces:**
- Consumes: Task 8 engine.
- Produces: `SyncEngine.resolve_pointer(pair, nb_obj) -> int` (inventree pk) — finds the enabled pairing mapping on the same connection for the pointer's content type, recursively `sync_object`s the referenced NetBox object under that pairing first (depth-first), returns its `SyncedObject.inventree_pk`; raises `PairingResolutionError` (subclass of engine error → object failed) when pairing missing/disabled. `SyncEngine.handle_delete(mapping, netbox_object_id) -> SyncedObject` — mark link `orphaned`, `deactivate` via PATCH `{"active": false}` when the endpoint's field specs contain writable `active` boolean, else log-only; never DELETE. `SyncEngine.verify_remote(mapping, synced) -> dict` — GET the InvenTree object, compare mapped fields against the freshly built payload, PATCH back on any difference (returns `{drifted: bool}`). `reconcile_mapping` composition, in order: `verify_remote` for every `synced` link (this is the authoritative drift repair the spec requires), `sync_object` for `pending`/`failed` links, `handle_delete` for links whose NetBox object is gone, `sync_object` for NetBox objects without links.
- [ ] **Step 1: Write failing tests** (fake client)

`test_company_created_before_part_references_it` (mapping part + pairing manufacturer→company; sync a NetBox InventoryItem with manufacturer; fake client records order: POST /api/company/ precedes PATCH/POST /api/part/ with `category`-style FK id equal to returned pk); `test_pairing_disabled_fails_object_without_post` (pairing mapping enabled=False → part sync failed, `last_error` mentions pairing; zero POST to /api/part/) — Review Focus 3; `test_reconcile_repairs_remote_drift` (synced link; fake client GET returns object with description different from NetBox payload → verify_remote PATCHes expected value; identical remote → no PATCH); `test_deactivate_on_delete` (link exists; delete NetBox obj → handle_delete → PATCH `{"active": false}`, status `orphaned`); `test_delete_without_active_field_logs_only` (endpoint specs lack `active` → no PATCH, log `deactivate` with note, status `orphaned`); `test_reconcile_flags_missing_netbox_objects`.
- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement in `engine.py`** (recursion depth guard: NetBox object graph ≤ 10; cycle path would be caught by validation, guard anyway).

- [ ] **Step 4: Run tests, verify pass** — PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add netbox_inventree_sync/sync/engine.py netbox_inventree_sync/tests/test_pointer_resolution.py
git commit -m "feat: pointer resolution with pairing order and delete-to-deactivate"
```

### Task 10: Signals, jobs, reconcile, management command

**Files:**
- Create: `netbox_inventree_sync/signals.py`, `netbox_inventree_sync/jobs.py`, `netbox_inventree_sync/management/commands/inventree_sync.py`
- Modify: `netbox_inventree_sync/__init__.py` (ready() connects; plugin queues none — use `default`), `netbox_inventree_sync/models.py` (SyncMapping.save schedules reconcile via enqueue_once)
- Test: `netbox_inventree_sync/tests/test_jobs.py`

**Interfaces:**
- Consumes: Task 8/9 engine; Task 3 models.
- Produces:
  - `jobs.ObjectSyncJob(JobRunner)` `Meta.name = "InvenTree Sync: Object"`; `run(mapping_id, content_type_id, object_id, deleted=False)` → `deleted=False` calls `engine.sync_object`, `deleted=True` calls `engine.handle_delete`.
  - `jobs.ReconcileJob(JobRunner)` `Meta.name = "InvenTree Sync: Reconcile"`; `run(mapping_id)` → engine.reconcile_mapping.
  - `signals.register_receivers()`/`unregister_receivers()` — `post_save` receiver enqueues `ObjectSyncJob(deleted=False)`, `post_delete` receiver enqueues `ObjectSyncJob(deleted=True)`, both bound to the content types of enabled mappings; receivers re-register when a mapping's `enabled` flag changes. Receivers must never raise into NetBox (wrap in try/except log).
  - `SyncMapping.save()`: when `enabled`, `ReconcileJob.enqueue_once(instance=self, interval=self.reconcile_interval_minutes)`.
  - Management command `manage.py inventree_sync` with flags: `--mapping <name>` (required), `--object <ct>:<pk>` (sync a single NetBox object inline), `--reconcile` (run `engine.reconcile_mapping` inline). With neither `--object` nor `--reconcile`: sync all `pending`/`failed` links of the mapping inline. Used by E2E for determinism. Exit code 1 if any processed object ended `failed`.
- [ ] **Step 1: Write failing tests**

`test_save_signal_enqueues_job` (enabled mapping on dcim.manufacturer; save a Manufacturer → a `Job` row exists for ObjectSyncJob, object tied); `test_disabled_mapping_no_job`; `test_delete_signal_deactivate_path` (fake client; run the enqueued job inline via job class `run` with `deleted=True` → orphan status); `test_mapping_save_schedules_reconcile` (enable mapping → scheduled Job with interval = mapping's); `test_command_runs_inline` (seed one Manufacturer; call_command('inventree_sync', mapping=..., object='dcim.manufacturer:<pk>') with fake client → POST recorded, exit ok; `--reconcile` variant exercises reconcile path). Note: tests assert Job rows via `core.models.Job`; RQ redis must be up (compose).
- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement signals/jobs/command + ready()**

`ready()` wraps receiver registration in `try/except` (DB may be unmigrated). Register also on `post_migrate`.

- [ ] **Step 4: Run tests, verify pass** — PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add netbox_inventree_sync/signals.py netbox_inventree_sync/jobs.py netbox_inventree_sync/management/ netbox_inventree_sync/tests/test_jobs.py netbox_inventree_sync/__init__.py netbox_inventree_sync/models.py
git commit -m "feat: signal-driven jobs, scheduled reconcile and inline sync command"
```

### Task 11: Plugin REST API + fields endpoint

**Files:**
- Create: `netbox_inventree_sync/api/__init__.py`, `api/serializers.py`, `api/views.py`, `api/urls.py`; Modify: `netbox_inventree_sync/urls.py` (mount), `__init__.py` (`api_version`? none needed)
- Test: `netbox_inventree_sync/tests/test_api.py`

**Interfaces:**
- Consumes: Task 3 models, Task 4 client, Tasks 5-6 introspection/compatibility, Task 7 validation.
- Produces (mounted under `/api/plugins/netbox_inventree_sync/`):
  - Read/write ModelViewSets: `connections/`, `mappings/` (nested writable `field_pairs`), `synced-objects/` (read), `sync-log/` (read). Serializers use `api_token` write-only.
  - `GET fields/?connection=<id>&content_type=<ct_id>&endpoint=<path>` → `{ "netbox": [NetboxFieldSpec dicts], "inventree": [InventreeFieldSpec dicts + `compatible_netbox_fields: [names]` per field], "available_pairings": ["dcim.manufacturer", ...] }` — compatibility computed per field via Task 6. Serializer `validate()` runs Task 7 `validate_mapping` on mappings writes (400 on incompatible).
  - `POST mappings/<id>/sync/` → enqueues ReconcileJob immediately ("Sync now").
- [ ] **Step 1: Write failing tests**

`test_fields_endpoint_matrix` (connection to fake client; assert `active` boolean lists no text netbox fields as compatible; `IPN` compatible with text fields), `test_mapping_write_rejects_incompatible_pair` (400 `incompatible`), `test_mapping_write_sets_transform`, `test_sync_now_enqueues_reconcile`, `test_token_write_only` (GET connections never returns `api_token`).
- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement API** (tutorial step 10 patterns; `HyperlinkedModelSerializer` + `NetBoxModelViewSet`).

- [ ] **Step 4: Run tests, verify pass** — PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add netbox_inventree_sync/api netbox_inventree_sync/tests/test_api.py netbox_inventree_sync/urls.py
git commit -m "feat: plugin rest api with compatibility fields endpoint"
```

### Task 12: UI — connections & mappings CRUD

**Files:**
- Create: `netbox_inventree_sync/forms.py`, `tables.py`, `filtersets.py`, `views.py`, `urls.py`, `navigation.py`, `templates/netbox_inventree_sync/{inventreeinstance,syncmapping}{,_list,_edit}.html`
- Test: `netbox_inventree_sync/tests/test_ui.py`

**Interfaces:**
- Consumes: Task 3 models, Task 11 API patterns not required (direct views).
- Produces: tutorial-style CRUD for both models: `InvenTreeInstanceListView/EditView/DeleteView/BulkDeleteView` + `SyncMapping*` equivalents; nav menu "InvenTree Sync" with Connections + Mappings buttons (`navigation.py`); Connection edit form has a `Test connection` submit (view calls `client.test_connection()`, messages success/error). Mapping form fields: name, connection, netbox_content_type (NetBox `ContentTypeChoiceField` filtered to change-logged models), inventree_endpoint, match fields, interval, enabled, description. Field pairs are edited on the builder screen (Task 13), not in this form.
- [ ] **Step 1: Write failing tests**

Django test client, logged-in superuser (`self.client.force_login`): `test_connections_list_renders`, `test_connection_create_and_test_button` (post valid form → object exists; GET edit page contains `Test connection`), `test_mapping_create_renders_content_type_choices` (page contains `dcim.inventoryitem`-style label), `test_nav_menu_present` (any page HTML contains `InvenTree Sync`).
- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement** per tutorial steps 3–8 (tables, forms, generic views, urls, nav).

- [ ] **Step 4: Run tests, verify pass** — PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add netbox_inventree_sync/forms.py netbox_inventree_sync/tables.py netbox_inventree_sync/filtersets.py netbox_inventree_sync/views.py netbox_inventree_sync/urls.py netbox_inventree_sync/navigation.py netbox_inventree_sync/templates netbox_inventree_sync/tests/test_ui.py
git commit -m "feat: connections and mappings crud ui with nav menu"
```

### Task 13: UI — field-pair builder screen

**Files:**
- Create: `netbox_inventree_sync/templates/netbox_inventree_sync/mapping_builder.html`, `netbox_inventree_sync/static/netbox_inventree_sync/js/mapping-builder.js`
- Modify: `views.py` (`SyncMappingBuilderView` — GET renders with initial data from Task 11 fields endpoint contract via server-side render; POST saves pairs through Task 7 validation), `urls.py`
- Test: `netbox_inventree_sync/tests/test_builder_ui.py`

**Interfaces:**
- Consumes: Task 11 fields endpoint data shape, Task 7 `validate_mapping`.
- Produces: the spec's two-column screen — NetBox fields left, InvenTree fields right, existing pairs rendered as linked rows; new pair row = NetBox select + InvenTree select; InvenTree options incompatible for the chosen NetBox field carry `disabled` + title `type mismatch: <nb> → <inv>` (JS uses the `compatible_netbox_fields` lists from the fields data); pointer NetBox field + string target reveals attribute select from `subfield_options`; save POSTs pairs (JSON) to the builder view which validates (400 → form errors inline) and writes `SyncFieldPair` rows.
- [ ] **Step 1: Write failing tests**

`test_builder_renders_both_columns` (GET builder for a mapping → HTML contains field names from both sides, e.g. `IPN` and `serial`), `test_incompatible_options_disabled` (HTML/JSON payload marks boolean `discovered` incompatible with `IPN` — assert the fields JSON served to the page lists `discovered` not in `IPN.compatible_netbox_fields`), `test_save_pairs_valid` (POST two valid pairs → rows created with transforms), `test_save_pairs_invalid_400` (boolean→string → response 400, zero rows).
- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement view + template + JS** (vanilla JS, no build step; NetBox base template extension).

- [ ] **Step 4: Run tests, verify pass** — PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add netbox_inventree_sync/templates/netbox_inventree_sync/mapping_builder.html netbox_inventree_sync/static netbox_inventree_sync/views.py netbox_inventree_sync/urls.py netbox_inventree_sync/tests/test_builder_ui.py
git commit -m "feat: field-pair builder screen with type-compat greying"
```

### Task 14: UI — synced objects, sync log, object panel

**Files:**
- Modify: `tables.py`, `views.py`, `urls.py`, `navigation.py`
- Create: `netbox_inventree_sync/template_content.py`, `templates/netbox_inventree_sync/{syncedobject_list,synclogentry_list,sync_panel}.html`
- Test: extend `netbox_inventree_sync/tests/test_ui.py`

**Interfaces:**
- Consumes: Task 3 models.
- Produces: read-only list views (NetBox table + filterset) for `SyncedObject` (filter: status, mapping; columns: object link (`mapping.netbox_content_type` + `netbox_object_id` → NetBox object URL), status badge, inventree link (`<connection.url>` + human path derived from endpoint + pk), last synced, error) and `SyncLogEntry` (filter: action, synced; expandable payload/response via detail modal or preformatted block). "Sync now" button on the mapping detail view (POST → enqueue `ReconcileJob`, success message). `template_content.py` `TemplateExtension` registered for every content type with enabled mappings: left-page panel `sync_panel.html` "InvenTree: Part #42 · synced 3 min ago" linking out. Nav buttons for both lists.

- [ ] **Step 1: Write failing tests**

`test_synced_objects_list_with_filters` (seed rows; filter status=failed → only those), `test_sync_log_renders_response_body` (row with body text appears), `test_mapping_detail_sync_now_enqueues_reconcile` (POST mapping detail sync button → ReconcileJob enqueued/scheduled immediately), `test_object_panel_shows_on_inventoryitem` (enabled mapping + synced row; GET the NetBox InventoryItem detail page → HTML contains `InvenTree` panel and link to `http://localhost:1337/part/42/`-style URL — construct as `<connection.url>` + endpoint-derived path).

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement** (template extension model list computed dynamically; register conservatively with try/except like signals).

- [ ] **Step 4: Run tests, verify pass** — PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add netbox_inventree_sync/template_content.py netbox_inventree_sync/templates netbox_inventree_sync/tables.py netbox_inventree_sync/views.py netbox_inventree_sync/urls.py netbox_inventree_sync/navigation.py netbox_inventree_sync/tests/test_ui.py
git commit -m "feat: sync status ui, log views and object detail panel"
```

### Task 15: GraphQL, search, starter templates

**Files:**
- Create: `netbox_inventree_sync/graphql/` (`__init__.py`, `schema.py`, `types.py`, `filters.py`), `search.py`, `management/commands/seed_starter_mappings.py`
- Test: `netbox_inventree_sync/tests/test_extras.py`

**Interfaces:**
- Consumes: Task 3 models, Task 11 client for validation.
- Produces: GraphQL types/filters for `InvenTreeInstance` + `SyncMapping` wired via PluginConfig `graphql_schema`; `search.py` `SearchIndex` subclasses for both; `seed_starter_mappings --connection <id>` creates the spec's disabled mapping set: Inventory Item → `/api/part/` (pairs: name→name, part_id→IPN, description→description, `manufacturer`+subfield `name`→keywords), Manufacturer → `/api/company/` (name→name, description→description), Inventory Item Role → `/api/part/category/` (name→name). Idempotent (skip existing by unique constraint).

- [ ] **Step 1: Write failing tests**

`test_seed_creates_three_disabled_mappings` (run command; assert rows + pairs incl. transform `str` on manufacturer.name→keywords; run again → no duplicates); `test_seed_validates_against_live_connection` (fake client specs; validation passes — pairs compatible); `test_search_indexes_registered` (`search.registered_searches` contains both models or registry equivalent); GraphQL: `test_graphql_schema_lists_connection_type` (schema contains type `InvenTreeInstance` with field `name`).

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement** per tutorial steps 11–12.

- [ ] **Step 4: Run tests, verify pass** — PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add netbox_inventree_sync/graphql netbox_inventree_sync/search.py netbox_inventree_sync/management/commands/seed_starter_mappings.py netbox_inventree_sync/tests/test_extras.py
git commit -m "feat: graphql, search indexes and starter mapping templates"
```

### Task 16: E2E suite + README

**Files:**
- Create: `tests/e2e/conftest.py`, `tests/e2e/test_sync_flows.py`, `tests/e2e/test_ui_smoke.py`; Modify: `README.md`
- Modify: `scripts/start.sh` (ensure worker + runserver supervision)

**Interfaces:**
- Consumes: everything; live stack (compose up, runserver, rqworker), seeded tokens; `manage.py inventree_sync` command (Task 10) for deterministic runs.
- Produces: `tests/e2e/conftest.py` fixtures: `netbox_api`/`inventree_api` (requests sessions with tokens from env/.env defaults), `mapping_set` (creates connection (URL from env) + three enabled mappings mirroring the starter set via plugin REST API), `cleanup` (deletes created NetBox objects + InvenTree parts/companies/categories by pk markers). Test module implements the spec's ten scenarios:
  1. `test_create_inventory_item_creates_part` — NetBox API create site/manufacturer/device/inventory item → run `inventree_sync` → InvenTree `/api/part/` contains part with `IPN == part_id`, `description`, `keywords` containing manufacturer name.
  2. `test_update_propagates` — change description → sync → part updated.
  3. `test_delete_deactivates_never_deletes` — delete item → sync → part `active == false`, still exists.
  4. `test_pointer_pairing_order` — manufacturer synced to company; part sync references nothing partial: company exists before part (assert via created timestamps or explicit order in one flow).
  5. `test_incompatible_pair_rejected_by_api` — POST mapping with boolean→string pair → 400; no InvenTree objects appear on sync.
  6. `test_adopt_existing_by_match_key` — pre-create InvenTree part with `IPN=X-EXISTING` → create NetBox item `part_id=X-EXISTING` → sync → same pk adopted (count of parts with that IPN == 1).
  7. `test_outage_marks_failed_and_reconcile_heals` — `docker compose stop inventree-web` → sync → NetBox synced-object `failed` via API; start container → run reconcile (command with `--mapping`) → status `synced`, part exists.
  8. `test_manual_inventree_edit_overwritten_by_reconcile` — PATCH InvenTree description directly → reconcile → original value restored.
  9. `test_custom_field_mapping` — NetBox custom field `supplier_code` (text) on InventoryItem, mapped →keywords; sync → value present.
  10. `test_signal_to_job_async_path` — with rqworker running: create item via API → poll NetBox `/api/core/jobs/` until the ObjectSyncJob completes (timeout 60s) → assert part in InvenTree (no manual command).
  `test_ui_smoke.py`: login session (form login with csrftoken) → mapping builder page renders both field lists and the fields JSON marks `discovered` incompatible with `IPN`.
- [ ] **Step 1: Write failing tests** (full suite; scenarios 7 needs docker control via subprocess)

- [ ] **Step 2: Run to verify failure**

Run: `cd netbox && ./venv/bin/python -m pytest ../tests/e2e -v` (stack up via `./scripts/start.sh` first)
Expected: FAIL (mappings API exists but flows not yet proven green; use failures as the fix list).

- [ ] **Step 3: Fix the chain until green** (fixes land in the responsible task's files; this task is the proving ground — treat any engine defect found as a bug fix with its own unit test where practical)

- [ ] **Step 4: Run full suite + full unit suite, verify pass**

Run: `cd netbox && ./venv/bin/python -m pytest ../tests/e2e -v && ./venv/bin/python manage.py test netbox_inventree_sync -v 1`
Expected: all E2E PASS, all unit PASS.

- [ ] **Step 5: Write README** (setup: `scripts/setup_netbox.sh`, `scripts/setup_inventree.sh`, `scripts/start.sh`; usage: configure connection → seed starter mappings → enable → builder screen; reconcile intervals; running tests incl. E2E env vars)

- [ ] **Step 6: Commit**

```bash
git add tests/e2e README.md scripts/start.sh
git commit -m "feat: end-to-end suite over live netbox+inventree stack"
```

---

## Task dependency order

1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12 → 13 → 14 → 15 → 16. Tasks 11–15 depend on 10 but are parallelizable among themselves after 11; 16 last.
