# NetBox ↔ InvenTree Sync Plugin — Design

Date: 2026-11-06
Status: Draft for approval

## Intent and why

The team keeps infrastructure inventory in NetBox (source of truth) and wants
InvenTree — a dedicated inventory/stock management system — to stay in sync
with it automatically. Today the same part data would have to be entered
twice. The plugin pushes changes from NetBox to InvenTree over InvenTree's
REST API whenever objects change, using field mappings the user configures in
the NetBox UI by linking a NetBox field to an InvenTree field (only
type-compatible pairs are offered).

The user has not yet decided how InvenTree will be used (spare-part stock vs.
serialized tracking vs. catalog mirror), so the engine is generic: what syncs
is entirely a matter of configuration, and the shipped starter template covers
the catalog case (NetBox Inventory Item → InvenTree Part).

## Goals

- One-way sync NetBox → InvenTree, event-driven (near-real-time), with
  periodic reconciliation and manual triggering.
- User-configurable mappings in the NetBox UI: pick a NetBox object type and
  an InvenTree API endpoint, then link field pairs.
- Type compatibility enforced in the UI: only compatible NetBox↔InvenTree
  field pairs can be linked (text→text, boolean→boolean, etc.).
- Full support for pointer (foreign-key) fields on both sides via
  object-level pairings, with automatic ID translation.
- Reliable object identity: a link table in NetBox maps each NetBox object to
  its InvenTree counterpart, surviving renames.
- Full observability: sync state per object, sync log with request/response
  detail, per-object panel in the NetBox UI, link to the InvenTree object.
- Local end-to-end test environment (NetBox + InvenTree) and an E2E test
  suite proving create/update/delete flows.

## Non-goals (v1)

- Two-way sync. InvenTree is never a source; edits there are overwritten on
  the next push.
- Syncing InvenTree stock levels, pricing, BOMs, or ordering data back into
  NetBox.
- UI for managing InvenTree-side schemas beyond what the API exposes.
- Automatic management of junction objects that InvenTree models separately
  (e.g. `ManufacturerPart` — a Part has no direct manufacturer FK; see
  Pointer resolution).

## Anti-goals

- Deleting InvenTree objects when the NetBox twin is deleted (destructive to
  stock history; we deactivate + flag instead).
- Hardcoding any specific object-type pair into the engine — everything is
  configuration.

## Constraints

- Plugin follows the official NetBox plugin tutorial structure and workflow:
  manual layout (`models.py`, `tables.py`, `forms.py`, `views.py`, `urls.py`,
  `navigation.py`, `filtersets.py`, `search.py`, `templates/<plugin>/`,
  `api/`, `graphql/`), developed against a local NetBox dev install
  (`DEBUG=True`, `DEVELOPER=True`), NetBox v4.5+, Python 3.12+.
  Reference: https://github.com/netbox-community/netbox-plugin-tutorial
- InvenTree is driven only through its public REST API (token auth), never
  its database. API v1 (`/api/...`), self-describing via `OPTIONS` metadata.
- Background work uses NetBox's job system (JobRunner / django-rq), not ad-hoc
  threads.
- Only officially documented NetBox plugin APIs are used.

## Approaches considered

### 1. What level to sync (catalog vs. instance vs. generic engine)

- **A. Catalog level** — each distinct part number becomes one InvenTree
  Part; serials/quantities stay in NetBox. Simple, covers "keep the parts
  catalog in sync", but bakes one philosophy in.
- **B. Instance level** — each NetBox Inventory Item becomes a serialized
  InvenTree StockItem plus an auto-created Part. Full fidelity, much more
  machinery (two linked objects per item, Part↔StockItem relationship).
- **C. Generic engine (chosen)** — a mapping is "any NetBox object type ↔
  any InvenTree endpoint + field pairs". A and B become configurations, not
  code. Costs more to build, but the field-pair mapping requirement already
  forces most of the machinery, and the user is undecided on usage, which
  makes flexibility the deciding factor.

### 2. Pointer (FK) field handling

- **A. Skip pointers** — plain values only. Too weak: manufacturer/category
  is half the value of the sync.
- **B. Pointers as text** — virtual fields like `manufacturer → name` mapping
  to InvenTree text fields. Cheap, but InvenTree pointer fields (e.g.
  `Part.category`) stay unusable.
- **C. Full pointers (chosen)** — object pairings + pointer→pointer mapping
  with ID translation through the link table. B is included as a sub-feature
  (any NetBox pointer can alternatively render one attribute as text).
  Most complex; accepted because category/manufacturer fidelity matters.

### 3. Test environment shape

- **All docker** (netbox-docker + InvenTree compose): realistic, but plugin
  development inside containers fights the tutorial's workflow.
- **Local NetBox dev install + docker for InvenTree and backing services
  (chosen)**: matches the tutorial exactly (git clone, venv, runserver with
  live plugin reload), keeps InvenTree (a separate Django app with
  conflicting dependencies) isolated in containers.
- Two local Django installs in separate venvs: rejected — InvenTree dev
  setup (frontend build, node) is heavyweight; its docker image is official
  and API access is all the plugin needs.

## Decisions (user)

- Generic engine (approach 1C); starter template for Inventory Item → Part.
- Full pointer support (approach 2C).
- Plugin built per the NetBox plugin development tutorial.

## The design

### Plugin identity

Package `netbox-inventree-sync`, Django app name `netbox_inventree_sync`,
plugin name `netbox_inventree_sync`. Top-level nav menu "InvenTree Sync".

### Data model (all models subclass NetBoxModel; tutorial step 2)

- **InvenTreeInstance (Connection)** — `name`, `url`, `api_token`
  (write-only, stored for the client), `enabled`. Multiple connections are
  allowed (e.g. prod + test InvenTree).
- **SyncMapping** — `name`, `connection` (FK), `netbox_content_type`
  (FK to Django ContentType), `inventree_endpoint` (e.g. `/api/part/`),
  `match_netbox_field` + `match_inventree_field` (identity pair for adopting
  existing InvenTree objects on first sync; "always create" if blank),
  `enabled`, `description`. Unique per (connection, netbox_content_type).
- **SyncFieldPair** — FK to mapping, `netbox_field`, `netbox_subfield`
  (attribute path for pointer-as-text, e.g. `name` for
  `manufacturer.name`), `inventree_field`, `transform` (none | iso_date |
  str, chosen automatically from the compatibility matrix). Validation:
  types must be compatible; an InvenTree field may be targeted by at most
  one pair per mapping.
- **SyncedObject (link table)** — FK to mapping, `netbox_object_id`,
  `inventree_pk`, `status` (pending | synced | failed | orphaned),
  `last_synced_at`, `last_payload_hash`, `last_error`. Unique per
  (mapping, netbox_object_id).
- **SyncLogEntry** — FK to SyncedObject (nullable), action (create | update |
  deactivate | adopt | error), payload snapshot, response status/body
  excerpt (API token redacted), created timestamps.

### Field introspection and compatibility

- NetBox side: iterate the model's concrete fields via Django `_meta`,
  plus custom fields (from NetBox's custom field registry) and tags where
  present. Each field carries: name, label, type, and whether it is a
  pointer (FK) and to which model.
- InvenTree side: `OPTIONS` request against the chosen endpoint returns
  field name, type (`string`, `boolean`, `integer`, `float`,
  `related field`), `required`, `read_only`, `max_length`, and for related
  fields the related model name. Read-only fields are excluded as targets.
- Compatibility matrix (the UI enforces this):

  | NetBox type | InvenTree `string` | `boolean` | `integer` | `float` | related |
  |---|---|---|---|---|---|
  | text (char/text/url/slug/email) | ✓ | – | – | – | – |
  | boolean | – | ✓ | – | – | – |
  | integer | – | – | ✓ | – | – |
  | decimal/float | – | – | – | ✓ | – |
  | date | ✓ (iso_date transform) | – | – | – | – |
  | choice/select fields | ✓ | – | – | – | – |
  | pointer (FK) | ✓ (as text, via chosen attribute) | – | – | – | ✓ (only if an object pairing exists for the target model) |
  | json, multiselect, object-multi | excluded in v1 | | | | |

- Custom fields participate with their NetBox types (`text`, `integer`,
  `boolean`, `date`, `url`, `select`; `object` custom fields behave as
  pointers).

### Pointer resolution (object pairings)

- A pointer→related pair is allowed only when another enabled SyncMapping
  exists whose netbox_content_type is the pointer's target model — this is
  the "object pairing" (e.g. `dcim.manufacturer → /api/company/`,
  `dcim.inventoryitemrole → /api/part/category/`).
- At push time, translating NetBox FK value → InvenTree PK: look up the
  SyncedObject link for that (pairing mapping, object id). If missing, the
  engine first ensures the referenced object itself is synced (depth-first
  resolution; the referenced object is pushed before the referencer).
  Cycles are rejected at mapping validation time.
- Known asymmetries are documented, not coded around: InvenTree `Part` has
  no direct manufacturer FK (that is `ManufacturerPart`, a separate object).
  For manufacturer on Parts, users map `manufacturer.name` as text (e.g. to
  `keywords`) in v1. Auto-managing junction objects is a v2 candidate.

### Sync engine flow

```
NetBox object saved/deleted
  └▶ signal receiver: enabled mapping for this content type?
       └▶ enqueue background job (RQ, low priority; one job per object change)
            └▶ build payload from field pairs (pointer resolution may push
               referenced objects first)
            └▶ link exists? → PATCH inventree object
               no link?    → match key lookup (GET endpoint?field=value)
                             hit  → adopt: store link + PATCH
                             miss → POST create → store link
            └▶ record SyncedObject status + SyncLogEntry
```

- **Deletes**: mark the SyncedObject `orphaned` and deactivate the InvenTree
  object (`PATCH {"active": false}` where supported) — never DELETE. Objects
  whose InvenTree model has no `active` flag are left untouched and logged.
- **Conflicts**: NetBox always wins; a manual InvenTree edit is overwritten
  by the next push or by reconcile.
- **Errors**: failed pushes set `status=failed` with `last_error`; the
  reconcile job retries failed and pending objects. No inline retries.
- **Reconcile (system job)**: enqueued at a configurable interval (default
  hourly; NetBox system jobs run while the RQ worker runs). Walks all
  enabled mappings, recomputes payload hashes, pushes drift, adopts
  matches, retries failures, reports orphans.
- **Manual**: "Sync now" button on each mapping (enqueues an immediate
  full pass for that mapping).

### Signals

`post_save`/`post_delete` receivers connected dynamically for the content
types of enabled mappings (connected at plugin ready + reconnected when
mappings change). Receivers only enqueue jobs — no API calls in the request
path.

### UI (tutorial steps 3–8 conventions)

- Menu "InvenTree Sync": Connections, Mappings, Synced Objects, Sync Log.
- Connection CRUD + "Test connection" (hits InvenTree `/api/` with the
  token; reports user + server version).
- Mapping create/edit is a builder flow:
  1. pick connection → pick NetBox object type → pick InvenTree endpoint
     (live-fetched list of endpoints from the connection's API schema);
  2. field-pair screen: two columns, NetBox fields left, InvenTree fields
     right; each row is a link with type badges; incompatible InvenTree
     fields render greyed with a tooltip ("type mismatch: boolean → string");
     pointer fields on the NetBox side offer an attribute dropdown when
     linked to a text target, or resolve via pairing when linked to a
     related target;
  3. match key selection; save + validate (compatibility, duplicate
     targets, pairing existence, cycles).
- Mapping detail: field pairs table, sync counts, recent log entries,
  "Sync now" button.
- Synced Objects: filterable table (status, mapping, last synced, error).
- Sync Log: table with expandable request/response detail.
- Template extension: panel on synced NetBox object pages —
  "InvenTree: Part #42 · synced 3 min ago · ✓" with a link to the InvenTree
  object URL.
- REST API (tutorial step 10) for all plugin models; GraphQL (step 11) and
  search indexes (step 12) for Connections and Mappings.

### REST API usage of InvenTree

- Token auth header; `inventree-python` is not used (thin `requests`-based
  client keeps the surface small and debuggable); timeouts and pagination
  handled by the client; the client caches `OPTIONS` metadata per endpoint
  (short TTL) to keep the mapping builder responsive.

### E2E environment (in this repository)

```
netbox-inventree-sync/
  docker-compose.yml        # postgres + redis for NetBox, InvenTree stack
  netbox/                   # git clone of netbox (v4.5+), dev config, venv
  netbox_inventree_sync/    # the plugin package (installed into netbox venv -e)
  tests/                    # unit + e2e tests
  scripts/                  # setup.sh: clones, venvs, tokens, seed data
```

- NetBox runs from `./netbox` via `runserver` (tutorial workflow); its
  Postgres and Redis run in docker compose; plugin installed editable into
  NetBox's venv so code changes reload.
- InvenTree runs fully in docker compose (server + worker + its own Postgres
  + Redis), admin user and API token seeded by script.
- One-time setup: `scripts/setup.sh`; daily start: `docker compose up -d`
  plus `./netbox/venv/bin/python netbox/manage.py runserver` and an rqworker.

### E2E tests (pytest, `tests/e2e`)

Run against the live pair (NetBox ORM/django test client + InvenTree API):
1. Create Inventory Item in NetBox → InvenTree Part exists with mapped
   fields.
2. Update a mapped field → Part reflects it.
3. Delete → Part deactivated, link orphaned, nothing deleted in InvenTree.
4. Pointer pairing: Manufacturers sync to Companies; Role → Part category
   resolves; dependency order correct (company created before part that
   references it).
5. Type incompatibility: mapping form/API rejects boolean→string pairs.
6. Match/adopt: pre-existing InvenTree part with matching key is adopted,
   not duplicated.
7. Failure and heal: InvenTree container stopped → push fails, status
   failed, error logged; container back + reconcile → converges.
8. Reconcile drift: manual edit in InvenTree is overwritten by reconcile.
9. Custom field mapping (text custom field → keywords).
10. UI smoke: mapping builder renders both field lists with greyed
    incompatible targets (browser automation, light-touch).

### Starter templates

On first run (management command or fixture), seed one disabled example
mapping set: Inventory Item → `/api/part/` (name→name, part_id→IPN,
description→description, `manufacturer.name`→keywords; serial is
per-instance and stays out of the catalog-level starter),
Manufacturer → `/api/company/`, Inventory Item Role → `/api/part/category/`.

## Left to the builder

- Exact base templates/styling for the field-pair builder screen (must look
  native NetBox; htmx/plain JS acceptable).
- Pagination limits and batch sizes for reconcile (start: page size 100,
  sequential).
- Caching TTL for OPTIONS metadata (start: 5 min).
- Whether SyncLogEntry needs retention pruning (start: none; note in docs).
- Request timeout defaults (start: 10s connect / 30s read).
- Migration naming, table column widths, and other cosmetic details per
  tutorial conventions.

## Milestones

1. Scaffold plugin (tutorial steps 0–1) + dev environment (NetBox runserver,
   docker backing, InvenTree compose) + plugin loads in NetBox.
2. Connection model + InvenTree API client + OPTIONS introspection + test
   connection UI.
3. Mapping models + field introspection (both sides) + compatibility matrix
   + validation.
4. Mapping builder UI (field-pair screen with greying).
5. Sync engine: link table, payload build, match/adopt, create/update,
   pointer resolution, signals → jobs.
6. Reconcile system job + manual sync + delete→deactivate + orphan handling.
7. Observability: Synced Objects/Log UI, object page panel, REST API,
   GraphQL, search.
8. E2E suite green on the live pair.
