# factotum-mcp

The shared, registry-driven MCP server for **Factotum**, the owner's general Mac
helper app (one instance per host: Host1 + Host2, bundle
`io.example.factotum`). Python stdlib at startup; the Postgres sync path
additionally uses psycopg from the service's own venv (see Registry sync
below). This repo also holds the VM callers for both hosts
(`callers/factotum-mcp-call` for the Host1,
`callers/factotum-mcp-call-host2` for Host2, plus the shared
`factotum_mcp_client.py` they pipe over SSH).

## Single-door posture

Factotum is the **manager** of each Mac's local tooling, and factotum-mcp
is the **single entry point** Postgres and agents use for Mac capabilities:
app discovery and CRUD actions, plus status/start/stop/restart/logs and a
health roll-up for every managed service on the host. Sibling MCPs
(photopull-mcp, shortcuts-mcp, anvilcapture-mcp, the logbook endpoint)
remain where they are and stay directly reachable during the transition;
the registry catalogs their actions and services so Factotum is the map
even before it is the only road.

## How it works

- Source of truth per host: `~/Library/Application Support/Factotum/registry.json`
  with three sections: `apps`, `actions`, `services` (schemas in the
  factotum repo's contract docs; the server re-reads the file per call).
- The server binds loopback `127.0.0.1:7901`, guarded by an origin secret
  in the service's `.env` on the host (`ORIGIN_SHARED_SECRET`, never
  committed, never printed; callers read it transiently on the host).
- Native action invokes and all service management dispatch to
  Factotum.app through its inbox/outbox file contract. The server refuses
  `op=delete` and `risk=delete|outward` actions outright: those require
  the owner's explicit approval in chat. Catalog actions return a pointer to the
  owning tool, never a proxied invocation (v1).
- Service live status (launchd state, pid, http/port/launchd health) is
  probed by the server itself, so `factotum_services` and
  `factotum_health` are truthful even while the app is mid-restart.

## Tools

| tool | purpose |
|---|---|
| `factotum_apps` | registry app list (installed/running, versions) |
| `factotum_actions` | registry CRUD action list, filterable by app |
| `factotum_invoke` | invoke a native action by id (gates above) |
| `factotum_registry` | the full registry document |
| `factotum_discovery_run` | ask the app to rediscover + rewrite the registry |
| `factotum_services` | managed services with live status |
| `factotum_service_invoke` | status/start/stop/restart/logs_recent via the app |
| `factotum_health` | one-call roll-up: registry freshness, app, MCP, all services, disk state |
| `factotum_registry_sync` | `push` syncs this host's registry to Postgres now; `status` compares local vs Postgres (hash, generated_at) and lists peers |
| `factotum_peer_registry` | the other host's registry, read from its Postgres replica |
| `factotum_parity` | cross-host diff of the two synced registries (apps / actions / services) |
| `screensharing_status` | Apple Screen Sharing state on this host (enabled, 5900 listening, allowed users/groups) |
| `screensharing_set` | turn Screen Sharing on/off (1.6.0); enabling restricts access to the configured screen-sharing user only; runs kickstart via a narrow sudoers drop-in (`/etc/sudoers.d/factotum-screensharing`) |
| `factotum_candidates` | Tier 2 candidate action records from capability research (1.7.0); researched proposals with evidence/confidence/risk class, status always candidate, never invocable |
| `factotum_capability_coverage` | capability coverage summary for this host + Postgres parity view of both hosts' synced capability sections (1.7.0) |
| `factotum_leo_asset_terms` | live leo query: every search term attached to one Photos asset (1.13.0) |
| `factotum_leo_term_assets` | live leo query: the Photos assets carrying a term, exact/prefix/contains, optional category (1.13.0) |
| `factotum_leo_stats` | leo store counts per library (main / Syndication) (1.13.0) |
| `factotum_vu_asset_observations` | live VUIndex query: a Photos asset's Visual Understanding observations with clustering mappings (1.13.0) |
| `factotum_vu_label_assets` | live VUIndex query: the assets carrying observations under one label id (1.13.0) |
| `factotum_vu_similar_assets` | live VUIndex query: nearest assets by embedding cosine similarity, computed on-host (1.13.0) |

## Photo-index live queries (1.13.0)

The leo_* and vu_* tools answer point queries against the host's
Photos ML stores directly from the server (the screensharing-probe
posture: host state read by the server itself, no app dispatch, no
registry action). Stores: the main library's
`database/search/leo.sqlite`, the Syndication library's leo.sqlite
(the `library` arg on the leo tools), and the main library's
`private/com.apple.mediaanalysisd/caches/vision/VUIndex.sqlite`.

Every connection opens the store with SQLite `mode=ro` (plus
`PRAGMA query_only = ON`): a write through these tools is
impossible at the SQLite layer. VUIndex embeddings (128-dim
float32) are compared on-host for `factotum_vu_similar_assets`
and never returned; only asset UUIDs and scores come back. All
result sets are bounded by explicit limits. The Postgres leo mirror
(`photo_search_terms`) stays the bulk-join surface; these tools
are the fresh, per-host point-query surface beside it.

## Capability research (1.7.0)

The factotum-capability companion (Factotum repo, install/) writes
`capabilities.json` in the support dir: per-app Tier 1 capability
hints from local evidence, plus Tier 2 candidate action records.
This server reads that state: `factotum_actions` appends
candidates as action views (status `candidate`), the two tools
above serve the records and the coverage/parity summary, and the
Postgres sync assembles the pushed document as registry.json + the
`capabilities` section so the replica carries the capability map.
`factotum_invoke` refuses any candidate before all other gates —
candidates are recorded, never run; outward/delete candidates
record identically and refuse identically. The app-side fold-in
of the section into registry.json itself rides the Factotum shell
release (specified in the capability build record).

## Disk roll-up (1.3.0)

`factotum_health` carries a `disk` block computed from the
registry's `disk` section (written by Factotum.app 1.3.0; see the
factotum repo's registry-schema doc): per-volume `free_pct`,
watched-path sizes with over-cap flags, the section's thresholds,
the last cleanup run, and a recomputed `state` — `warn` when any
volume's free < 15%, `critical` when < 8% or a watched path exceeds
its cap — also surfaced as `summary.disk_state` (`unknown` when the
registry predates the section). The state is recomputed from the
section's numbers, never trusted from the app. The server also
honors the per-service freshness budget `health.threshold_s` in
log-freshness probes (fleet default 26h; the Logbook capture daemon
carries 300s), and the app gains two ops the invoke path maps from
the cleanup self actions: `cleanup_dry_run` (starts factotum-cleanup
detached in dry-run mode) and `cleanup_last_run` (the newest run
record). The cleanup tool's `--apply` is unreachable through this
server by design.

## Registry sync

Each host is authoritative for its own `registry.json`. This server
syncs a replica into Postgres Postgres (db `postgres`, tables `factotum_*`;
DDL in `server/postgres-schema.sql`) at startup (background thread), after
every discovery run, and on demand via `factotum_registry_sync push`.

Since 1.2.0 (Oct 17, 2026):

- **App sync requests:** a watcher thread polls
  `~/Library/Application Support/Factotum/sync-requests/` every ~5s.
  Factotum.app drops a request there whenever discovery completes
  (boot, daily cadence, its menu/window) or its user clicks Sync Now;
  the watcher consumes the file (deleted before the sync runs) and
  pushes with reason `app_request:<reason>`.
- **Settings gate:** if `postgres_sync_enabled` is `false` in the app's
  `config.json`, `sync_push` skips (returns
  `{ok: true, skipped: "sync_disabled"}`) and Postgres is untouched.
- **Status file:** every attempt is recorded to
  `~/Library/Application Support/Factotum/sync-status.json`
  (attempt time, ok, reason, error, content hash, the registry's
  `generated_at`, the enabled flag); the app's menu and window read
  it for their sync display.
One transaction per sync: the snapshot row (full registry jsonb +
content hash) upserts and this host's normalized rows in
`factotum_apps` / `factotum_actions` / `factotum_services` are replaced;
a sha256 over the canonical JSON short-circuits an unchanged registry
to a `synced_at` touch. A host only ever writes rows keyed by its own
host label (`FACTOTUM_HOST`).

Since 1.14.1 (internal ticket), the assembled document includes valid newer
exercise records from this host's `canary-state.json`. Before either the
hash comparison or a row replacement, the sync locks this host's Postgres
registry row and reconciles any newer complete canary record already in
the registry blob or normalized action rows. Missing or malformed
incoming fields are not an erase instruction. A failed history read stops
the sync before any write; `canary_note` reports only counts, not details.
The same transaction keeps the blob and normalized rows consistent.

Connection: `FACTOTUM_DB_DSN` in the service `.env`, role
`factotum_sync` (grants on the four factotum_* tables only). The
password is pipeline-managed and lives only in host `.env` files (0600);
it is never logged or returned by any tool. psycopg is imported lazily
inside the sync path, so a missing DSN or driver degrades the three
sync tools to plain "not configured / unavailable" results and leaves
every other tool untouched.

## Deploy (per host)

1. Copy `server.py` to `~/services/factotum-mcp/server.py` on the host.
2. Create the sync venv: `python3 -m venv ~/services/factotum-mcp/.venv`
   and `~/services/factotum-mcp/.venv/bin/pip install "psycopg[binary]"`.
3. Create `~/services/factotum-mcp/.env` (0600) with `PORT=7901`,
   `ORIGIN_SHARED_SECRET=<generated on the host>`,
   `FACTOTUM_HOST=<host1|host2>`, and
   `FACTOTUM_DB_DSN=<the factotum_sync DSN, provisioned per host>`.
4. Install LaunchAgent `io.example.factotum-mcp` (RunAtLoad + KeepAlive)
   with the interpreter set to
   `~/services/factotum-mcp/.venv/bin/python`. After editing
   the plist, `launchctl bootout` + `bootstrap` it: `kickstart -k`
   restarts with the previously loaded job definition and will not pick
   up a new interpreter.
5. Install the caller for that host into `~/bin/` on the VM,
   alongside `factotum_mcp_client.py`.
6. Verify: `factotum_registry_sync {action:"status"}` shows
   `in_sync: true`, and `factotum_parity` names the peer honestly until
   it has synced.

## Config-as-data

This server is an implementation of the config-as-data doctrine (project doctrine; canonical text in the Postgres `docs/agent-doctrine.md`
section "Config-as-Data Doctrine" and in the factotum repo's
`docs/CONFIG-AS-DATA.md`): the host's `registry.json` is the local
working copy, the Postgres `factotum_*` tables are the system of record for
seeing and coordinating, and the registry sync above is what keeps the
two in step.
