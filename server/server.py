#!/usr/bin/env python3
"""factotum-mcp: registry-driven MCP front-end for Factotum, the owner's general
Mac helper app (one instance per host: Host1 + Host2).

Runs on the host (loopback 127.0.0.1:7901). The source of truth is the
host's Factotum registry:

    ~/Library/Application Support/Factotum/registry.json

    {host, generated_at,
     apps:    [{bundle_id, name, version, path, installed, running}],
     actions: [{id, app_bundle_id, name, op, mechanism, risk, status,
                notes, pointer?}]}

  op:     read|create|update|delete
  risk:   read|write|delete|outward
  status: native   (implemented by Factotum.app on this host)
          catalog  (owned by a sibling tool; registry documents it and
                    "pointer" says where it lives -- v1 does not proxy)
          unavailable

The MCP never invents capabilities: factotum_apps / factotum_actions /
factotum_registry are views over the registry file, re-read on every call
so a discovery refresh is visible immediately. factotum_invoke dispatches
only native actions (to Factotum.app via the inbox/outbox file contract);
it refuses op=delete and risk=delete/outward actions outright ("requires
the owner approval"), and returns catalog pointers instead of invoking them.

Factotum.app IPC contract (files, like PhotoPull/AnvilCapture; the app
is the shared Swift build from the factotum repo, and its protocol is
op-based):
  inbox/<id>.json   {"id", "op": <op>, "params": {...}}   (params are
                    merged flat by the app; flat top-level params also work)
  outbox/<id>.json  the op's result dict plus {"id", "op", "ok": bool};
                    failures carry an "error" string.
Ops in v1: health, registry_read, discovery_run, logs_recent,
restart_hint, apps_running, app_launch, app_quit, notes_recent,
notes_create, mail_create_draft, service_status, service_start,
service_stop, service_restart, service_logs_recent, cleanup_dry_run,
cleanup_last_run (the last two are v1.3.0: factotum-cleanup's dry
run, started detached by the app, and its newest run record; the
script's --apply is never reachable through this server by design),
packages_plan, packages_apply, packages_last_run (v1.4.0:
factotum-pkg's read-only plan and its policy-bounded apply, both
started detached by the app, and the newest pkg run record; apply
executes the tool's auto class only, under the on-host policy in
pkg-config.json, and never sudo — exposed through the server at
the owner's direction, unlike cleanup's apply).

The registry also carries a `disk` section (v1.3.0): per-volume
free/total, watched-path sizes, alert thresholds, and the computed
state (ok | warn when any volume's free < 15% | critical when any
volume's free < 8% or a watched path exceeds its stated cap).
factotum_health rolls it into the one-call health answer, and the
section syncs to Postgres inside the registry snapshot like everything
else.

Since v1.4.0 the registry also carries a `packages` section
(written by factotum-pkg via the app): per-manager versions,
top-level inventory (brew formulae/casks with cask policy classes,
npm globals, pipx, uv tools, mas apps), classified outdated lists,
the policy (hold lists) and the update schedule. factotum_health
rolls it in as a packages signal (ok | outdated with count |
stale when the section is older than 36h), and the sync flattens
it into the normalized factotum_packages table when that table
exists (DDL in server/postgres-schema.sql).

A small set of self actions is answered by the MCP itself from its own
view of the host (factotum.health, factotum.registry_read,
factotum.logs_recent): they report on the registry file, the support
folder, and the MCP process, so they work even before the app answers.

Factotum is also the MANAGER of the host's Mac-local tooling, and this
MCP is the single entry point Postgres and agents use for Mac capabilities
(sibling MCPs stay directly reachable during the transition, but the
registry + this server are the canonical door). The registry therefore
also carries a services section:

    services: [{id, launchd_label, kind, repo, desired_state,
                health: {type, target}, actions[]}]

  kind: app | mcp-server | daemon | supervised | ingest-job
  health.type: http (GET target URL) | port (TCP connect to target port)
               | launchd (label loaded/running) | none
  actions: any of status|start|stop|restart|logs_recent

Tools:
  factotum_apps            {running_only?, query?}  registry app list
  factotum_actions         {app?}                  registry action list
  factotum_invoke          {action_id, params?}    native dispatch only
  factotum_registry        {}                      full registry document
  factotum_discovery_run   {}                      ask the app to rediscover
  factotum_services        {}                      services + live status
  factotum_service_invoke  {service_id, action}    via Factotum.app
  factotum_health          {}                      one-call roll-up
  factotum_registry_sync   {action: push|status}   Postgres registry sync
  factotum_peer_registry   {host?}                 peer's synced registry
  factotum_parity          {}                      cross-host registry diff
  factotum_candidates      {app?}                  Tier 2 candidate actions
  factotum_capability_coverage {}                  capability coverage + parity
  factotum_leo_asset_terms {asset, library?}       leo terms for one asset
  factotum_leo_term_assets {term, category?,       assets holding a leo term
                            match?, library?}
  factotum_leo_stats       {library?}              leo store counts
  factotum_vu_asset_observations {asset}           VUIndex obs for one asset
  factotum_vu_label_assets {label, limit?}         assets under a VU label
  factotum_vu_similar_assets {asset, limit?}       nearest assets by embedding

Photo-index live queries (v1.13.0, internal ticket): the leo_* and vu_*
tools answer point queries against this host's Photos ML stores
(the main library's leo.sqlite search index, the Syndication
library's leo.sqlite, and the main library's VUIndex.sqlite visual
understanding index) directly from this server, the same posture as
the screensharing probes: host state read by the server itself,
never by dispatching to the app. Every connection opens the store
with SQLite mode=ro, so a write through these tools is impossible
at the SQLite layer; embeddings are read and compared on-host and
never leave the Mac (the Postgres leo mirror stays the bulk-join
surface; these tools are the fresh, point-query surface beside
it). Results are bounded by explicit limits; there is no
full-store dump shape.

Capability research (v1.7.0): the factotum-capability companion
(~/services/factotum/factotum-capability, a Factotum-repo install/
tool) writes ~/Library/Application Support/Factotum/capabilities.json:
per-app Tier 1 capability hints from local evidence plus researched
Tier 2 candidate action records (status=candidate). This server
serves both: factotum_actions appends candidates as action views,
factotum_candidates returns the raw records, and
factotum_capability_coverage summarizes coverage for this host plus
the Postgres-synced parity view. Candidates are NEVER invocable:
factotum_invoke refuses status=candidate before any dispatch, and
outward/delete candidates are recorded, never run (promotion is a
human/operator decision per action). The Postgres sync assembles the
pushed document as registry.json + a `capabilities` section from
the companion's state file, so the replica carries the capability
map freshness-stamped like the rest of the registry; the app-side
fold-in of the section into registry.json itself landed with the
Factotum shell release (app 1.6.0).

Shell release (v1.8.0, app 1.6.0): the registry gains top-level
`permissions` (grant state as data; the shell is the truth
source), `payload` (payload version + state), and
`inbox_depth`/`outbox_depth`, and each services[] entry may carry
the shell's attested `live` block. The sync has always carried
the whole registry document as the factotum_registries blob, so
the new sections ride unchanged (proven against a staged
registry on both hosts before the shell shipped); 1.8.0 adds the
reader surface: factotum_health passes the new sections through,
factotum_services surfaces `registry_live` beside its own probe,
and factotum_invoke knows the shell's notify / payload_reload /
payload_rollback ops.

Not-yet-run job state (v1.9.0): a job probe used to score a job
that has not run since boot as a failure whenever its evidence
was absent — but its evidence (a log under /tmp) does not survive
the reboot either, so every reboot painted the host's scheduled
ingest jobs red until each one's next run, hours later. The probe
now asks launchd for the job's recorded run count in this boot
session (`launchctl print`, since `launchctl list` reports
LastExitStatus 0 for never-run jobs): a loaded job with zero
recorded runs whose verdict would rest on absent evidence
returns a neutral state="not_yet_run", ok=None — visible in
factotum_health, tallied separately in the summary, and outside
the health denominator. Any recorded exit, or any evidence on
disk, and the pre-1.9.0 logic applies unchanged. Companion fix on
the hosts: the ingest jobs' stdout/evidence logs moved from /tmp
to ~/Library/Logs, so evidence itself now survives reboots.

v1.10.0 corrects three stored-state defects and adds two health
riders. (1) The not_yet_run anchor is the agent's LOAD, not the
host boot: the runs counter and StartInterval clock reset at
every bootstrap, and the 1.9.0 "since boot" wording misread
healthy long-interval jobs as suspect after the Oct 18
evidence-path re-bootstrap. (2) A job whose current instance is
running (a KeepAlive daemon) is never failed on the retained
exit record, which belongs to the previous instance; the
verdict keys on current-instance liveness plus evidence
freshness, with the previous exit reported as history. (3)
Stored service live blocks decay: a registry_live verdict older
than twice the shell's 15-minute probe cadence renders as
indeterminate (ok=None, last_attested preserved) instead of
asserting its stale success — applied both at the services /
health surface and in the assembled Postgres document. Riders:
the health summary gains outbox_depth with an outbox_backlog
flag (>500) and a packages_stale flag when the packages
measured_at is older than 26h. The payload (1.6.0-p6) mirrors
(1) and (2) in its own job probe, and the shell (1.6.3) carries
the neutral verdict into the stored registry and stamps
generated_at on every registry content change.

v1.11.0 lands the canary fields as queryable data (Factotum 2.0
Phase 0(a), internal ticket): a Postgres schema migration adds
last_exercised_at / last_exercise_outcome / last_exercise_detail
columns to factotum_actions, and the sync now carries them from
the registry blob into the normalized rows on every push, so
exercise state is visible to SQL, monitoring rules, and staleness
checks instead of only to blob readers. The sync probes for the
columns and degrades to the pre-1.11.0 column set when they are
absent, so either deploy order (migration first or server first)
is safe; actions never exercised carry NULLs.

Registry sync (project design note): this host is authoritative for its
own registry.json. This server syncs a replica into Postgres Postgres
(host1, db postgres, tables factotum_*; DDL in postgres-schema.sql beside
this file) at server startup, after every discovery run, and on demand
via factotum_registry_sync. A host only ever writes its own rows. The
Postgres copies exist for cross-host visibility, parity checks
(factotum_parity), and future coordination; factotum_peer_registry
reads them without contacting the peer host. Connection comes from
FACTOTUM_DB_DSN in this service's .env (role factotum_sync,
pipeline-managed password, never logged). psycopg is imported lazily
inside the sync path so server startup stays stdlib-only; when the DSN
is unset or psycopg is unavailable, the sync tools report that plainly
and every other tool is unaffected.
"""
import glob
import hashlib
import json
import os
import re
import socket
import sqlite3
import struct
import subprocess
import threading
import time
import urllib.request
import uuid as uuidlib
from array import array
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from operator import mul
from urllib.parse import quote

VERSION = "1.14.2"
HERE = os.path.dirname(os.path.abspath(__file__))
SUPPORT = os.path.expanduser("~/Library/Application Support/Factotum")
INBOX = os.path.join(SUPPORT, "inbox")
OUTBOX = os.path.join(SUPPORT, "outbox")
REGISTRY = os.path.join(SUPPORT, "registry.json")
FACTOTUM_LOG = os.path.join(SUPPORT, "factotum.log")
BOOT = time.time()


def load_env():
    env = {}
    try:
        with open(os.path.join(HERE, ".env")) as f:
            for line in f:
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


ENV = load_env()
PORT = int(ENV.get("PORT", "7901"))
SECRET = ENV.get("ORIGIN_SHARED_SECRET", "")
HOST_LABEL = ENV.get("FACTOTUM_HOST", socket.gethostname().split(".")[0].lower())
DB_DSN = ENV.get("FACTOTUM_DB_DSN", "")
SUPPORT = ENV.get("FACTOTUM_SUPPORT_DIR", SUPPORT)
INBOX = os.path.join(SUPPORT, "inbox")
OUTBOX = os.path.join(SUPPORT, "outbox")
REGISTRY = os.path.join(SUPPORT, "registry.json")
FACTOTUM_LOG = os.path.join(SUPPORT, "factotum.log")
# App <-> MCP sync transport (v1.2.0): Factotum.app's menu/window ask
# for a sync by dropping a request file into SYNC_REQUESTS_DIR (the
# mirror image of the app's inbox); the watcher thread below consumes
# them. Every sync attempt is also recorded to SYNC_STATUS (a small
# JSON file in the support dir) so the app's UI can display the last
# outcome without holding any secret or speaking HTTP.
CONFIG = os.path.join(SUPPORT, "config.json")
CAPABILITIES = os.path.join(SUPPORT, "capabilities.json")
CAPABILITY_TOOL = os.path.expanduser("~/services/factotum/factotum-capability")
SYNC_REQUESTS_DIR = os.path.join(SUPPORT, "sync-requests")
SYNC_STATUS = os.path.join(SUPPORT, "sync-status.json")

TOOLS = [
    {
        "name": "factotum_apps",
        "description": "List apps in this host's Factotum registry (installed apps discovered on the host, with bundle id, version, path, installed/running flags). Args: running_only (bool), query (substring filter on name/bundle id). The registry is refreshed by Factotum discovery (daily cadence + on demand); generated_at says how fresh it is.",
        "inputSchema": {"type": "object", "properties": {
            "running_only": {"type": "boolean"},
            "query": {"type": "string"}}},
    },
    {
        "name": "factotum_actions",
        "description": "List registered CRUD actions in this host's Factotum registry, optionally filtered by app (bundle id or app name substring). Each action carries op (read|create|update|delete), risk (read|write|delete|outward), status (native|catalog|unavailable) and, for catalog actions, a pointer to the owning tool. Candidate actions from capability research (status=candidate, served from the factotum-capability companion's state) are appended and counted in candidates_included; candidates are proposals for review and are never invocable.",
        "inputSchema": {"type": "object", "properties": {
            "app": {"type": "string"}}},
    },
    {
        "name": "factotum_invoke",
        "description": "Invoke one registered action by action_id. Native actions dispatch to Factotum.app on this host. Catalog actions are NOT invoked: the owning tool's pointer is returned instead. Actions with op=delete or risk=delete/outward are refused: they require the owner's approval in chat first.",
        "inputSchema": {"type": "object", "properties": {
            "action_id": {"type": "string"},
            "params": {"type": "object"}},
                        "required": ["action_id"]},
    },
    {
        "name": "factotum_registry",
        "description": "Return this host's full Factotum registry document (host, generated_at, apps, actions) exactly as stored.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "factotum_discovery_run",
        "description": "Ask Factotum.app to rediscover installed/running apps and capabilities and rewrite the registry now (it also runs daily on a cadence). Returns completed=true with the new generated_at when the app answers; otherwise queued=true and the registry keeps its previous stamp.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "factotum_services",
        "description": "List the Mac-local services Factotum manages on this host (from the registry's services section), each enriched with live status probed by the MCP: launchd loaded/running + pid, and the service's health check (http/port/launchd) result. Factotum is the manager and single entry point for Mac-local tooling; sibling MCPs remain directly reachable during the transition.",
        "inputSchema": {"type": "object", "properties": {
            "service_id": {"type": "string"}}},
    },
    {
        "name": "factotum_service_invoke",
        "description": "Manage one registered service through Factotum.app: action status|start|stop|restart|logs_recent|update. Update is registered only for Bumblebee and requires expected_sha256 and expected_commit for a staged binary. Only actions listed in the service registry are accepted.",
        "inputSchema": {"type": "object", "properties": {
            "service_id": {"type": "string"},
            "action": {"type": "string",
                       "enum": ["status", "start", "stop", "restart", "logs_recent", "update"]},
            "expected_sha256": {"type": "string"},
            "expected_commit": {"type": "string"}},
                        "required": ["service_id", "action"]},
    },
    {
        "name": "factotum_health",
        "description": "One-call health roll-up for this host: registry freshness (generated_at + age), Factotum app state, this MCP's state, every managed service's live state and health result (v1.5.0 success probes: job probes name their evidence — last exit status, freshness age vs the per-job budget, last success line; v1.9.0: a job with no recorded run since agent load and no evidence reports a neutral not_yet_run state (ok=null) instead of a failure, tallied separately in the summary; v1.10.0: a running KeepAlive daemon's verdict keys on its current instance, never on the previous instance's retained exit; stored registry_live blocks older than 2x the probe cadence render decayed/indeterminate; the summary gains outbox_depth + outbox_backlog (>500) and packages_stale (measured_at older than 26h); services with desired_state=stopped are reported stopped_as_desired and excluded from the health_ok denominator), disk state (per-volume free %, watched-path sizes; warn when a volume's free < 15%, critical when < 8% or a watched path exceeds its cap), and packages state (ok / outdated keyed on ACTIONABLE auto-class outdated only, with report-only classes counted separately and expired exception records surfaced as packages_exceptions_due), with a summary count by state.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "factotum_registry_sync",
        "description": "Sync this host's Factotum registry into Postgres Postgres (this host is authoritative for its own registry; only its own rows are written). action=push syncs now (a content hash short-circuits unchanged registries); action=status reports local vs Postgres generated_at/content_hash for this host plus each peer's synced_at. Sync also runs automatically at server startup and after discovery runs.",
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["push", "status"]}},
                        "required": ["action"]},
    },
    {
        "name": "factotum_peer_registry",
        "description": "Read the OTHER host's Factotum registry from its synced Postgres replica (no contact with the peer host). Optional arg host names which synced registry to return; with no arg, returns the peer's. Says plainly when the peer has not synced yet.",
        "inputSchema": {"type": "object", "properties": {
            "host": {"type": "string"}}},
    },
    {
        "name": "factotum_parity",
        "description": "Compare the two hosts' synced registries in Postgres: apps present on only one host (by bundle id), actions differing in op/risk/status (by action id, plus actions present on only one host), and services differing in desired_state (by service id, plus one-sided services). When only one host has synced, reports exactly that instead of a diff.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "factotum_candidates",
        "description": "List Tier 2 candidate action records from capability research (factotum-capability companion state on this host). Candidates are researched proposals with evidence, confidence and risk class; status is always candidate and they are never invocable (factotum_invoke refuses them). Optional arg app filters by bundle id or app name substring.",
        "inputSchema": {"type": "object", "properties": {
            "app": {"type": "string"}}},
    },
    {
        "name": "factotum_capability_coverage",
        "description": "Capability coverage summary: this host's Tier 1 assessment (apps assessed, apps with surfaces, surfaces by type, candidates by risk class, measured_at) from the factotum-capability companion, plus the Postgres parity view comparing both hosts' synced capability sections (assessment deltas, candidate ids present on one host only). Local-only with a plain note when Postgres sync is not configured or unreachable.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "screensharing_status",
        "description": "Report Apple Screen Sharing state on this host: enabled (screensharingd running and/or port 5900 accepting connections), listening_5900, and who may connect (allowed_users and allowed_nested_groups from the com.apple.access_screensharing group). Read-only, unprivileged probes.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "screensharing_set",
        "description": "Turn Apple Screen Sharing on or off on this host. Idempotent; returns the resulting status. Enabling restricts access to the configured screen-sharing user only (kickstart -configure -access -on -users <user> -privs -all). The state change runs the ARDAgent kickstart binary via sudo -n under a narrowly-scoped sudoers drop-in (/etc/sudoers.d/factotum-screensharing, that one binary only); without the drop-in the call fails cleanly and says so.",
        "inputSchema": {"type": "object", "properties": {
            "enabled": {"type": "boolean"}},
                        "required": ["enabled"]},
    },
    {
        "name": "factotum_leo_asset_terms",
        "description": "Live leo query on THIS host: every search term attached to one Photos asset, read from the host's leo.sqlite (the Apple Photos search index) at call time. Args: asset (the asset's UUID, required), library (main | syndication, default main), limit (default 200, max 1000). A lexeme names a concept and the lexicon holds one row per surface form: all forms are returned as {lexeme_id, category, content, canonical} in the index's own order (canonical = the type-1 form), excluding empty-content structural lexemes (counted in lexeme_count). The store's size/mtime is included so freshness is visible beside the Postgres mirror. Read-only (store opened mode=ro).",
        "inputSchema": {"type": "object", "properties": {
            "asset": {"type": "string"},
            "library": {"type": "string", "enum": ["main", "syndication"]},
            "limit": {"type": "integer"}},
                        "required": ["asset"]},
    },
    {
        "name": "factotum_leo_term_assets",
        "description": "Live leo query on THIS host: the Photos assets carrying a given search term, read from the host's leo.sqlite at call time. Args: term (required), match (exact | prefix | contains, default exact; all case-insensitive against lexicon content), category (optional integer category filter), library (main | syndication, default main), limit (default 100, max 500). Assets are leo items of type 1 or 6 (the mirror's asset grain); holders of any other item type are counted separately as other_items. Returns the matched lexemes and the asset UUIDs per lexeme. Read-only (store opened mode=ro).",
        "inputSchema": {"type": "object", "properties": {
            "term": {"type": "string"},
            "match": {"type": "string", "enum": ["exact", "prefix", "contains"]},
            "category": {"type": "integer"},
            "library": {"type": "string", "enum": ["main", "syndication"]},
            "limit": {"type": "integer"}},
                        "required": ["term"]},
    },
    {
        "name": "factotum_leo_stats",
        "description": "Counts for this host's leo.sqlite stores: lexicon rows, content-bearing lexemes, items, asset items (type 1/6), distinct categories, plus store size/mtime. Arg: library (main | syndication | all, default all). Read-only.",
        "inputSchema": {"type": "object", "properties": {
            "library": {"type": "string", "enum": ["main", "syndication", "all"]}}},
    },
    {
        "name": "factotum_vu_asset_observations",
        "description": "Live VUIndex query on THIS host: the Visual Understanding observations recorded for one Photos asset in the main library's VUIndex.sqlite. Args: asset (UUID, required), limit (default 50, max 200). Each observation carries its type/source/confidence, which embedding kinds it holds (the embeddings themselves are never returned), and its clustering mapping: label id, partition, similarity to its partner observation, and the partner's asset when resolvable. Read-only (store opened mode=ro).",
        "inputSchema": {"type": "object", "properties": {
            "asset": {"type": "string"},
            "limit": {"type": "integer"}},
                        "required": ["asset"]},
    },
    {
        "name": "factotum_vu_label_assets",
        "description": "Live VUIndex query on THIS host: the Photos assets that carry at least one observation under a clustering label id (labels are the store's integer ids, e.g. one taken from factotum_vu_asset_observations). Args: label (integer, required), limit (default 100, max 500). Returns one row per asset with its observation count under the label and its best similarity. Read-only (store opened mode=ro).",
        "inputSchema": {"type": "object", "properties": {
            "label": {"type": "integer"},
            "limit": {"type": "integer"}},
                        "required": ["label"]},
    },
    {
        "name": "factotum_vu_similar_assets",
        "description": "Live VUIndex query on THIS host: the nearest other Photos assets to one asset by embedding cosine similarity, computed on this host from VUIndex.sqlite embeddings (128-dim float32); embeddings never leave the Mac and are never returned. Each asset is represented by its primary, highest-confidence embedding-bearing observation. Args: asset (UUID, required), limit (default 10, max 50). Read-only (store opened mode=ro).",
        "inputSchema": {"type": "object", "properties": {
            "asset": {"type": "string"},
            "limit": {"type": "integer"}},
                        "required": ["asset"]},
    },
]


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def load_registry():
    reg = read_json(REGISTRY)
    if reg is None:
        return None
    if not isinstance(reg, dict) or "apps" not in reg or "actions" not in reg:
        return None
    return reg


# --- capability research (v1.7.0) -------------------------------------------
# The factotum-capability companion owns capabilities.json in the
# support dir: Tier 1 hints per app + Tier 2 candidate records. This
# server only READS that state (the companion is its writer), serves
# it, and merges it into the Postgres sync document.

_CANDIDATE_RISK_TO_REGISTRY = {"read": "read", "local-write": "write",
                               "outward": "outward", "delete": "delete"}
_CANDIDATE_RISK_TO_OP = {"read": "read", "local-write": "update",
                         "outward": "update", "delete": "delete"}


def load_capabilities():
    cap = read_json(CAPABILITIES)
    if cap is None or not isinstance(cap, dict):
        return None
    return cap


def candidate_records():
    cap = load_capabilities()
    if not cap:
        return []
    return [c for c in cap.get("candidates", []) if isinstance(c, dict)]


def assembled_registry(reg, canary_state=None):
    """The document the Postgres sync pushes: the on-disk registry
    plus the companion's `capabilities` section when its state
    exists. Since the Factotum shell release (app 1.6.0) the app
    folds the section into registry.json itself, so the merge here
    is a same-content no-op kept for pre-1.6.0 registries. Every
    other section the shell writes (permissions, payload, live,
    depths) rides the document unchanged: the sync stores the
    whole registry as the factotum_registries blob."""
    if reg is None:
        return None
    out = dict(reg)
    svcs = out.get("services")
    if isinstance(svcs, list):
        # Item 5(b): stored live blocks decay in the pushed
        # document too, so the Postgres replica the monitor's R1 rules
        # read never asserts a stale success (v1.10.0).
        decayed = []
        for s in svcs:
            if isinstance(s, dict) and isinstance(s.get("live"), dict):
                s = dict(s)
                s["live"] = _decay_live_block(s["live"])
            decayed.append(s)
        out["services"] = decayed
    if canary_state is None:
        canary_state, _ = _load_canary_state()
    out, _ = _overlay_canary_records(out, canary_state)
    cap = load_capabilities()
    if not cap:
        return out
    out["capabilities"] = cap
    return out


def candidate_action_view(c):
    """A candidate record shaped like a registry action, so listing
    surfaces treat it uniformly. status is always "candidate"."""
    risk_class = c.get("risk_class") or "read"
    return {"id": c.get("id"),
            "app_bundle_id": c.get("app_bundle_id"),
            "name": c.get("description"),
            "op": _CANDIDATE_RISK_TO_OP.get(risk_class, "read"),
            "mechanism": c.get("surface"),
            "risk": _CANDIDATE_RISK_TO_REGISTRY.get(risk_class, "read"),
            "status": "candidate",
            "risk_class": risk_class,
            "surface": c.get("surface"),
            "invocation": c.get("invocation"),
            "evidence": c.get("evidence"),
            "confidence": c.get("confidence"),
            "source": "factotum-capability"}


def maybe_kick_capability_scan():
    """Fire-and-forget: if the companion is installed and its state
    is missing or older than 26h, start a scan detached. The
    companion also runs daily on its own LaunchAgent; this keeps
    hints tracking discovery without any app-side wiring. Never
    raises, never blocks."""
    try:
        if not os.path.isfile(CAPABILITY_TOOL):
            return
        cap = load_capabilities()
        stale = True
        if cap and cap.get("measured_at"):
            try:
                ts = datetime.strptime(
                    cap["measured_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc)
                stale = (datetime.now(timezone.utc) - ts).total_seconds() \
                    > 26 * 3600
            except Exception:
                stale = True
        if not stale:
            return
        subprocess.Popen([CAPABILITY_TOOL, "scan"],
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception:
        pass


def queue(payload):
    req_id = uuidlib.uuid4().hex[:12]
    payload = dict(payload)
    payload["id"] = req_id
    os.makedirs(INBOX, exist_ok=True)
    tmp = os.path.join(INBOX, "." + req_id + ".tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.rename(tmp, os.path.join(INBOX, req_id + ".json"))
    return req_id


def wait_final(req_id, timeout_s):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        data = read_json(os.path.join(OUTBOX, req_id + ".json"))
        if data is not None:
            return data
        time.sleep(0.25)
    return None


def app_call(payload, timeout_s):
    req_id = queue(payload)
    data = wait_final(req_id, timeout_s)
    if data is None:
        return None, req_id
    out = {k: v for k, v in data.items() if k != "id"}
    # The transport id names this call in factotum.log's op lines.
    # An app result may carry its own request_id with domain
    # meaning (the photos engine's async job ids, shell 1.6.9):
    # the app's value wins and the transport id rides under its
    # own key, instead of silently overwriting the job id and
    # orphaning the caller's poll handle (fold-in fix, 1.14.1).
    if "request_id" in out:
        out["app_request_id"] = req_id
    else:
        out["request_id"] = req_id
    return out, req_id


# --- self actions the MCP answers from its own view ---------------------

def self_health(reg):
    out = {"ok": True, "host": HOST_LABEL, "mcp_version": VERSION,
           "mcp_uptime_s": round(time.time() - BOOT, 1),
           "registry_present": reg is not None,
           "support_dir": SUPPORT,
           "inbox_depth": len(glob.glob(os.path.join(INBOX, "*.json"))),
           "outbox_depth": len(glob.glob(os.path.join(OUTBOX, "*.json")))}
    if reg is not None:
        out["registry_generated_at"] = reg.get("generated_at")
        out["apps_registered"] = len(reg.get("apps", []))
        out["actions_registered"] = len(reg.get("actions", []))
    return out


def self_logs_recent(params):
    n = int(params.get("lines", 50))
    if not os.path.exists(FACTOTUM_LOG):
        return {"ok": True, "lines": [], "note": "no factotum.log yet at " + FACTOTUM_LOG}
    with open(FACTOTUM_LOG, "rb") as f:
        data = f.read().decode("utf-8", "replace")
    lines = [ln for ln in data.splitlines() if ln.strip()]
    return {"ok": True, "lines": lines[-n:], "total_lines": len(lines)}


MCP_LOCAL = {
    "factotum_health": lambda reg, params: self_health(reg),
    "factotum_registry_read": lambda reg, params: {"ok": True, "registry": reg},
    "factotum_logs_recent": lambda reg, params: self_logs_recent(params),
}

KNOWN_OPS = {"health", "registry_read", "discovery_run", "logs_recent",
             "restart_hint", "apps_running", "app_launch", "app_quit",
             "notes_recent", "notes_create", "mail_create_draft",
             "service_status", "service_start", "service_stop",
             "service_restart", "service_logs_recent", "service_update",
             "cleanup_dry_run", "cleanup_last_run",
             "packages_plan", "packages_apply", "packages_last_run",
             # Shell release (app 1.6.0): notify + payload management.
             "notify", "payload_reload", "payload_rollback",
             # Shell 1.6.5: break-glass lifecycle ops.
             "breakglass_status", "breakglass_enable",
             "breakglass_disable", "breakglass_rotate",
             # Shell 1.6.9: PhotoPull fold-in — the photos engine is
             # native to the shell; photopull.* registry actions map
             # onto these ops (PHOTOPULL_ACTION_OPS below).
             "photos_health", "photos_fetch", "photos_status",
             "photos_cancel", "photos_info", "photos_info_batch",
             "photos_preview", "photos_frame", "photos_search",
             "photos_recent", "photos_albums", "photos_album_assets",
             "photos_set_location", "photos_set_date"}

# The fold-in keeps the established photopull.* action ids (payload
# 1.6.0-p13 flips them from catalog pointers to native); their shell
# ops carry the photos_* names the 1.6.8 dispatch speaks.
PHOTOPULL_ACTION_OPS = {
    "photopull.photopull_fetch": "photos_fetch",
    "photopull.photopull_status": "photos_status",
    "photopull.photopull_cancel": "photos_cancel",
    "photopull.photopull_asset_info": "photos_info",
    "photopull.photopull_info_batch": "photos_info_batch",
    "photopull.photopull_preview": "photos_preview",
    "photopull.photopull_frame": "photos_frame",
    "photopull.photopull_search": "photos_search",
    "photopull.photopull_recent": "photos_recent",
    "photopull.photopull_albums": "photos_albums",
    "photopull.photopull_album_assets": "photos_album_assets",
    "photopull.photopull_health": "photos_health",
    "photopull.photopull_set_location": "photos_set_location",
    "photopull.photopull_set_date": "photos_set_date",
}


def op_for_action(action_id):
    """Map a registry action id to the app's inbox op name.

    App convention: factotum_<op> for self/service actions, bare <op>
    for the rest (apps_running, notes_create, ...)."""
    if action_id in PHOTOPULL_ACTION_OPS:
        return PHOTOPULL_ACTION_OPS[action_id]
    if action_id in KNOWN_OPS:
        return action_id
    if action_id.startswith("factotum_"):
        rest = action_id[len("factotum_"):]
        if rest in KNOWN_OPS:
            return rest
    return None


# --- tools ---------------------------------------------------------------

def tool_apps(args):
    reg = load_registry()
    if reg is None:
        return {"ok": False, "error": "registry missing or unreadable at " + REGISTRY}
    apps = reg.get("apps", [])
    if args.get("running_only"):
        apps = [a for a in apps if a.get("running")]
    q = (args.get("query") or "").lower()
    if q:
        apps = [a for a in apps
                if q in (a.get("name") or "").lower()
                or q in (a.get("bundle_id") or "").lower()]
    return {"ok": True, "host": reg.get("host", HOST_LABEL),
            "generated_at": reg.get("generated_at"),
            "count": len(apps), "apps": apps}


def tool_actions(args):
    reg = load_registry()
    if reg is None:
        return {"ok": False, "error": "registry missing or unreadable at " + REGISTRY}
    actions = reg.get("actions", [])
    app = (args.get("app") or args.get("app_bundle_id") or "").lower()
    if app:
        actions = [a for a in actions
                   if app in (a.get("app_bundle_id") or "").lower()
                   or app in (a.get("app_name") or "").lower()]
    cands = [candidate_action_view(c) for c in candidate_records()]
    if app:
        cands = [c for c in cands
                 if app in (c.get("app_bundle_id") or "").lower()]
    return {"ok": True, "host": reg.get("host", HOST_LABEL),
            "generated_at": reg.get("generated_at"),
            "count": len(actions) + len(cands),
            "candidates_included": len(cands),
            "actions": actions + cands}


def tool_registry(args):
    reg = load_registry()
    if reg is None:
        return {"ok": False, "error": "registry missing or unreadable at " + REGISTRY}
    return reg


def tool_candidates(args):
    cap = load_capabilities()
    if cap is None:
        return {"ok": False, "host": HOST_LABEL,
                "error": ("no capability state at " + CAPABILITIES +
                          "; factotum-capability has not run on this "
                          "host yet")}
    cands = candidate_records()
    app = (args.get("app") or args.get("app_bundle_id") or "").lower()
    if app:
        apps_by_id = {a.get("bundle_id"): (a.get("name") or "")
                      for a in cap.get("apps", [])}
        cands = [c for c in cands
                 if app in (c.get("app_bundle_id") or "").lower()
                 or app in apps_by_id.get(c.get("app_bundle_id"),
                                          "").lower()]
    return {"ok": True, "host": cap.get("host", HOST_LABEL),
            "measured_at": cap.get("measured_at"),
            "candidates_updated_at": cap.get("candidates_updated_at"),
            "count": len(cands), "candidates": cands}


def _capability_summary_of(cap):
    if not cap:
        return None
    return {"measured_at": cap.get("measured_at"),
            "summary": cap.get("summary"),
            "candidate_ids": sorted(c.get("id") for c in
                                    cap.get("candidates", [])
                                    if isinstance(c, dict))}


def tool_capability_coverage(args):
    cap = load_capabilities()
    out = {"ok": cap is not None, "host": HOST_LABEL.lower(),
           "local": _capability_summary_of(cap)}
    if cap is None:
        out["error"] = ("no capability state at " + CAPABILITIES +
                        "; factotum-capability has not run on this "
                        "host yet")
    if not DB_DSN:
        out["postgres"] = None
        out["note"] = ("FACTOTUM_DB_DSN is not set in this host's "
                       "factotum-mcp .env; parity view unavailable, "
                       "local summary only")
        return out
    try:
        snaps = postgres_snapshots()
    except RuntimeError as e:
        out["postgres"] = None
        out["note"] = str(e)
        return out
    hosts = {}
    for snap in snaps:
        reg = snap.get("registry") or {}
        hosts[snap["host"]] = {
            "synced_at": snap["synced_at"],
            "registry_generated_at": snap["generated_at"],
            "capabilities": _capability_summary_of(
                reg.get("capabilities"))}
    out["postgres"] = hosts
    mine = hosts.get(HOST_LABEL.lower(), {}).get("capabilities")
    peers = {h: v for h, v in hosts.items()
             if h != HOST_LABEL.lower()}
    if mine and peers:
        parity = {}
        for h, v in peers.items():
            theirs = v.get("capabilities")
            if not theirs:
                parity[h] = {"note": ("peer has synced a registry "
                                      "without a capabilities "
                                      "section")}
                continue
            my_ids = set(mine.get("candidate_ids") or [])
            their_ids = set(theirs.get("candidate_ids") or [])
            my_apps = (mine.get("summary") or {}).get("apps_assessed")
            their_apps = (theirs.get("summary") or {}).get(
                "apps_assessed")
            parity[h] = {
                "apps_assessed": {"local": my_apps, "peer": their_apps,
                                  "delta": (my_apps - their_apps)
                                  if isinstance(my_apps, int)
                                  and isinstance(their_apps, int)
                                  else None},
                "candidates_only_local": sorted(my_ids - their_ids),
                "candidates_only_peer": sorted(their_ids - my_ids)}
        out["parity"] = parity
    return out


def tool_invoke(args):
    action_id = args.get("action_id") or args.get("id") or ""
    params = args.get("params") or {}
    reg = load_registry()
    if reg is None:
        return {"ok": False, "error": "registry missing or unreadable at " + REGISTRY}
    action = None
    for a in reg.get("actions", []):
        if a.get("id") == action_id:
            action = a
            break
    if action is None:
        # Candidate actions (capability research) are listed by
        # factotum_actions but live in the companion's state, not
        # the registry. They are proposals for review: NEVER
        # invocable, whatever their risk class — recorded, not run.
        for c in candidate_records():
            if c.get("id") == action_id:
                return {"ok": False, "refused": True,
                        "error": ("action %s is a candidate "
                                  "(status=candidate) and is not "
                                  "invocable" % action_id),
                        "reason": ("candidate actions are researched "
                                   "proposals recorded for review; "
                                   "promotion to a live action is a "
                                   "human/operator decision, per action, "
                                   "and never happens through invoke"),
                        "candidate": c}
        return {"ok": False, "error": "unknown action_id: " + action_id}
    # Candidate status refuses before every other gate: a candidate
    # is not invocable whatever its op/risk say.
    if action.get("status") == "candidate":
        return {"ok": False, "refused": True,
                "error": ("action %s has status=candidate and is not "
                          "invocable" % action_id),
                "reason": ("candidate actions are researched proposals "
                           "recorded for review; promotion to a live "
                           "action is a human/operator decision, per "
                           "action, and never happens through invoke"),
                "action": action}
    # Hard gates next, for every live status.
    if action.get("security_class") == "credential":
        # Credential-class actions (break-glass enable/rotate) gate
        # under the D10 preview-token rule, which lives in
        # factotum-gateway. This plane has no preview flow, so it
        # refuses and names the gated path — same posture as the
        # delete/outward hard refusal below.
        return {"ok": False, "refused": True,
                "error": "requires D10 preview via factotum-gateway",
                "reason": ("action %s is credential-class "
                           "(security_class=credential); it is gated "
                           "under the D10 preview-token rule in "
                           "factotum-gateway and is not invocable on "
                           "the factotum-mcp plane" % action_id),
                "action": action}
    if action.get("op") == "delete" or action.get("risk") in ("delete", "outward"):
        return {"ok": False, "refused": True,
                "error": "requires the owner approval",
                "reason": ("action %s has op=%s risk=%s; Factotum never runs "
                           "delete or outward-facing actions without the owner's "
                           "explicit approval in chat" % (
                               action_id, action.get("op"), action.get("risk"))),
                "action": action}
    if action.get("status") == "catalog":
        return {"ok": True, "invoked": False, "catalog": True,
                "note": ("catalog action: owned by a sibling tool, not "
                         "Factotum. Use the pointer."),
                "pointer": action.get("pointer") or action.get("tool"),
                "action": action}
    if action.get("status") == "unavailable":
        return {"ok": False, "error": "action registered as unavailable on this host",
                "action": action}
    op = op_for_action(action_id)
    if op is None:
        return {"ok": False, "error": ("native action %s has no app op "
                                        "mapping" % action_id),
                "action": action}
    # Native: dispatch to Factotum.app through the file contract.
    timeout = 210 if op == "service_update" else 180 if op == "discovery_run" else 150 if op.startswith("photos_") else 90
    result, req_id = app_call({"op": op, "params": params}, timeout)
    if result is None:
        if action_id in MCP_LOCAL:
            out = MCP_LOCAL[action_id](reg, params)
            out["served_by"] = "mcp_fallback_app_not_answering"
            if "request_id" in out:
                out["app_request_id"] = req_id
            else:
                out["request_id"] = req_id
            out["action"] = action
            return out
        return {"ok": False, "error_kind": "app_not_answering",
                "error": ("Factotum.app did not answer invoke of %s (op %s) "
                          "in %ds; it may not be running yet on this host" % (
                              action_id, op, timeout)),
                "request_id": req_id, "action": action}
    result.setdefault("invoked", True)
    result["action"] = action
    return result


def tool_discovery_run(args):
    reg = load_registry()
    before = reg.get("generated_at") if reg else None
    result, req_id = app_call({"op": "discovery_run"}, 180)
    if result is None:
        return {"ok": True, "completed": False, "queued": True,
                "request_id": req_id, "generated_at": before,
                "note": ("Factotum.app did not answer a discovery request in "
                         "180s; the request stays queued in the inbox and the "
                         "registry keeps its previous stamp")}
    reg2 = load_registry()
    after = reg2.get("generated_at") if reg2 else None
    maybe_kick_capability_scan()
    trigger_sync_async("discovery_run")
    return {"ok": True, "completed": True, "request_id": req_id,
            "generated_at": after,
            "apps_registered": len(reg2.get("apps", [])) if reg2 else None,
            "actions_registered": len(reg2.get("actions", [])) if reg2 else None,
            "app_result": result}


# --- managed services ----------------------------------------------------

def launchd_status(label):
    """Probe launchd for one label in the user's GUI domain.

    Returns {loaded, running, pid, last_exit_status} using
    `launchctl list <label>` exit code + output. Runs locally on
    the host (the MCP lives there). last_exit_status is launchd's
    LastExitStatus for the most recent completed run (None when
    the job has never exited since load): the raw material of the
    success probes (v1.5.0)."""
    try:
        r = subprocess.run(["launchctl", "list", label], capture_output=True,
                           text=True, timeout=10)
    except Exception as e:
        return {"loaded": None, "running": None, "error": str(e)}
    if r.returncode != 0:
        return {"loaded": False, "running": False, "pid": None,
                "last_exit_status": None}
    pid = None
    last_exit = None
    for line in r.stdout.splitlines():
        line = line.strip().rstrip(";")
        if line.startswith('"PID"'):
            try:
                pid = int(line.split("=", 1)[1].strip())
            except Exception:
                pass
        elif line.startswith('"LastExitStatus"'):
            try:
                last_exit = int(line.split("=", 1)[1].strip())
            except Exception:
                pass
    return {"loaded": True, "running": pid is not None, "pid": pid,
            "last_exit_status": last_exit}


_BOOT_TIME = {"tried": False, "iso": None}


def _boot_time_iso():
    """This boot's start as an ISO-8601 UTC string (v1.9.0), from
    sysctl kern.boottime; cached (a boot time cannot change under a
    running server). None when undeterminable."""
    if _BOOT_TIME["tried"]:
        return _BOOT_TIME["iso"]
    _BOOT_TIME["tried"] = True
    try:
        r = subprocess.run(["sysctl", "-n", "kern.boottime"],
                           capture_output=True, text=True, timeout=10)
        m = re.search(r"sec\s*=\s*(\d+)", r.stdout or "")
        if m:
            _BOOT_TIME["iso"] = datetime.fromtimestamp(
                int(m.group(1)), timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        pass
    return _BOOT_TIME["iso"]


def _job_runs(label):
    """launchd's recorded run count for one label since the
    agent was loaded (v1.9.0; the counter resets at every
    bootstrap, which is the not_yet_run anchor — v1.10.0),
    parsed from `launchctl print
    gui/<uid>/<label>` (`runs = N`; its `last exit code` reads
    "(never exited)" when N is 0). `launchctl list` cannot answer
    this: it reports LastExitStatus 0 for jobs that have never run.
    None when undeterminable — callers must treat None as 'no
    information', never as 0."""
    try:
        r = subprocess.run(["launchctl", "print",
                            "gui/%d/%s" % (os.getuid(), label)],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    m = re.search(r"^\s*runs = (\d+)", r.stdout or "", re.M)
    return int(m.group(1)) if m else None


def _not_yet_run(ev):
    """Neutral verdict (v1.9.0): the job is loaded but has no
    recorded run since the agent was loaded and the evidence its
    verdict would rest on is absent, so there is nothing to
    judge yet. ok=None keeps it out of the health denominator
    entirely (the same neutral slot the launchd-probe-failure
    path uses).

    v1.10.0 anchor correction: the 1.9.0 text anchored on the
    host's BOOT time ("not yet run since boot"), but the runs
    counter the state keys on — and the StartInterval clock —
    both reset at every agent bootstrap. After a re-bootstrap
    (the Oct 18 evidence-path one) healthy long-interval jobs
    read as suspect for up to a full interval. The operative
    anchor is the agent's LOAD; launchctl reports no load
    timestamp, so the counter itself is the evidence and the
    reason says "since agent load"."""
    ev.update(ok=None, state="not_yet_run", anchor="agent_load",
              reason="not yet run since agent load (launchctl "
                     "runs = 0); no run recorded and the "
                     "evidence is absent")
    return ev


def _json_field_match(body, dotted):
    cur = body
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _job_probe(health):
    """Success probe for a launchd job (v1.5.0). target is the
    label; the spec also carries log, heartbeat, success_pattern and
    threshold_s. ok iff: loaded, last exit status is 0 (or never
    exited and currently running), fresh evidence exists
    (heartbeat mtime preferred, else log mtime, within
    threshold_s), and the success_pattern appears in the log's tail
    when one is configured. The result names the evidence.

    v1.9.0: a loaded job with NO recorded run since the agent
    was loaded (launchctl print runs = 0; the counter and the
    StartInterval clock reset at every bootstrap, so the anchor
    is the agent load, not the host boot — the 1.9.0 reason text
    said "since boot" and misread healthy long-interval jobs as
    suspect after any re-bootstrap) whose verdict would rest on
    absent evidence (no heartbeat/log file, or the pattern-check
    log file missing) returns the neutral not_yet_run state
    instead of a failure — there is simply nothing to judge
    until the job's first run after its load. Any recorded
    exit, or any evidence on disk, and the rules above apply
    unchanged.

    v1.10.0: a job whose CURRENT instance is running (a
    KeepAlive daemon) is never failed on the retained exit
    record: that record belongs to the previous instance. The
    verdict keys on current-instance liveness plus the evidence
    rules below, and the previous exit rides along as
    previous_exit_status history."""
    label = health.get("target")
    st = launchd_status(label)
    ev = {"type": "job", "target": label,
          "last_exit_status": st.get("last_exit_status"),
          "state": ("running" if st.get("running") else
                    "loaded" if st.get("loaded") else "not loaded"),
          "pid": st.get("pid"),
          "threshold_s": health.get("threshold_s")}
    if st.get("loaded") is None:
        ev.update(ok=None, reason="launchd probe failed: %s"
                  % st.get("error"))
        return ev
    if not st.get("loaded"):
        ev.update(ok=False, reason="job not loaded in launchd")
        return ev
    runs = _job_runs(label)
    never_ran = (runs == 0 and not st.get("running"))
    last_exit = st.get("last_exit_status")
    running_now = bool(st.get("running"))
    if running_now and last_exit not in (0, None):
        # KeepAlive daemon (v1.10.0): the retained exit record
        # belongs to the PREVIOUS instance (Host2 ingest-contacts
        # read ok=false "last run exited 256" a day into a
        # healthy 26h run because its previous instance had
        # crash-looped). The current instance is alive, so the
        # verdict comes from the evidence rules below; the old
        # exit is history.
        ev["previous_exit_status"] = last_exit
    exit_ok = (last_exit == 0) or (last_exit is None and running_now)
    if not exit_ok and not running_now \
            and not (never_ran and last_exit is None):
        # A never-run job has no exit to judge (its list-reported
        # status is a placeholder); it falls through to the
        # evidence rules, which return not_yet_run when the
        # evidence is absent too.
        ev.update(ok=False,
                  reason="last run exited %s (not success)" % last_exit)
        return ev
    threshold = health.get("threshold_s")
    if threshold is None:
        ev.update(ok=False,
                  reason="no threshold_s configured (the fleet "
                         "default was removed in v1.5.0); a budget "
                         "must come from the job's real cadence")
        return ev
    age = None
    src = path_used = None
    for candidate, source in ((health.get("heartbeat"), "heartbeat"),
                              (health.get("log"), "log")):
        if candidate:
            try:
                age = time.time() - os.path.getmtime(
                    os.path.expanduser(candidate))
                src, path_used = source, candidate
                break
            except OSError:
                continue
    ev.update(evidence_source=src, evidence_path=path_used,
              evidence_age_s=int(age) if age is not None else None)
    if age is None:
        if never_ran:
            return _not_yet_run(ev)
        if running_now:
            ev.update(ok=False,
                      reason="current instance running (pid %s) but "
                             "no heartbeat or log evidence found"
                             % st.get("pid"))
            return ev
        ev.update(ok=False,
                  reason="no heartbeat or log evidence found")
        return ev
    if age > threshold:
        ev.update(ok=False,
                  reason="evidence %ds old, budget %ds (%s stale)"
                         % (int(age), int(threshold), src))
        return ev
    pattern = health.get("success_pattern")
    if pattern and health.get("log"):
        last_line = None
        log_missing = False
        try:
            with open(os.path.expanduser(health["log"]), "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 262144))
                tail = f.read().decode("utf-8", "replace")
            hits = [ln.strip() for ln in tail.splitlines()
                    if pattern in ln]
            if hits:
                last_line = hits[-1][:200]
        except OSError:
            log_missing = True
        ev["last_success_line"] = last_line
        if last_line is None:
            if never_ran and log_missing:
                return _not_yet_run(ev)
            ev.update(ok=False,
                      reason="no %r success line in the log tail"
                             % pattern)
            return ev
    head = "last exit 0" if last_exit == 0 \
        else "current instance running"
    reason = ("%s; %s %ds old (budget %ds); success evidence present"
              % (head, src, int(age), int(threshold)))
    if "previous_exit_status" in ev:
        reason += ("; previous instance exited %s (history)"
                   % ev["previous_exit_status"])
    ev.update(ok=True, reason=reason)
    return ev


def health_probe(health):
    if not health:
        return {"ok": None, "type": None}
    htype = health.get("type")
    target = health.get("target")
    if htype == "job":
        return _job_probe(health)
    if htype == "http":
        status = None
        raw = b""
        try:
            req = urllib.request.Request(target, method="GET")
            with urllib.request.urlopen(req, timeout=4) as resp:
                status = resp.status
                raw = resp.read(262144)
        except Exception as e:
            code = getattr(e, "code", None)
            if code:
                status = code
            else:
                return {"ok": False, "type": "http", "target": target,
                        "error": str(e)}
        expect = health.get("expect_json") or {}
        if not expect:
            # v1.5.0: no expectations means a bare 200 check. The
            # old status<500 rule read a 404 as healthy (the
            # logbook-host2 / probe did exactly that).
            return {"ok": status == 200, "type": "http",
                    "target": target, "http_status": status}
        try:
            body = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return {"ok": False, "type": "http", "target": target,
                    "http_status": status, "expect_json": expect,
                    "reason": "HTTP %s but body is not JSON" % status}
        matched = {}
        mismatched = []
        for dotted, want in expect.items():
            got = _json_field_match(body, dotted)
            matched[dotted] = got
            if got != want:
                mismatched.append("%s=%r (want %r)" % (dotted, got,
                                                       want))
        ok = status == 200 and not mismatched
        return {"ok": ok, "type": "http", "target": target,
                "http_status": status, "expect_json": expect,
                "matched": matched,
                "reason": ("HTTP %d, expected fields match" % status
                           if ok else "HTTP %d; mismatched: %s"
                           % (status, "; ".join(mismatched)))}
    if htype == "port":
        try:
            with socket.create_connection(("127.0.0.1", int(target)), timeout=3):
                return {"ok": True, "type": "port", "target": target}
        except Exception as e:
            return {"ok": False, "type": "port", "target": target,
                    "error": str(e)}
    if htype == "launchd":
        st = launchd_status(target)
        return {"ok": bool(st.get("loaded")), "type": "launchd",
                "target": target, "loaded": st.get("loaded"),
                "last_exit_status": st.get("last_exit_status"),
                "pid": st.get("pid")}
    if htype == "log-freshness":
        path = os.path.expanduser(target)
        # v1.5.0: threshold_s is REQUIRED. The 26h fleet default
        # hid dead jobs behind a blanket budget; a freshness budget
        # must come from the service's real cadence (the Logbook
        # capture daemon's 300s is the pattern).
        raw_threshold = health.get("threshold_s")
        if raw_threshold is None:
            return {"ok": False, "type": "log-freshness",
                    "target": target,
                    "reason": "no threshold_s configured (the fleet "
                              "default was removed in v1.5.0)"}
        try:
            threshold = int(raw_threshold)
        except (TypeError, ValueError):
            return {"ok": False, "type": "log-freshness",
                    "target": target,
                    "reason": "threshold_s is not an integer: %r"
                              % raw_threshold}
        try:
            age = time.time() - os.path.getmtime(path)
            return {"ok": age < threshold, "type": "log-freshness",
                    "target": target, "age_s": round(age),
                    "threshold_s": threshold}
        except Exception as e:
            return {"ok": False, "type": "log-freshness", "target": target,
                    "error": str(e)}
    return {"ok": None, "type": htype, "target": target}


LIVE_STALE_AFTER_S = 1800  # 2x the shell's 15-minute probe cadence


def _decay_live_block(live):
    """Item 5(b) (v1.10.0): a stored live block attests the moment
    it was probed; rendered long afterwards it must not keep
    asserting that moment's success. When the block is older
    than twice the shell's 15-minute probe cadence and its
    verdict claims ok — or the shell already marked the block
    stale because a probe round could not evaluate it — the
    rendered verdict decays to indeterminate (ok None) with the
    stale attestation named; the original verdict rides along
    as last_attested so nothing is hidden. The shell (the
    writer) re-probes on its cadence; this decay is the
    read-side backstop applied at both render points: the
    factotum_services/factotum_health registry_live surface
    and the Postgres document assembled_registry pushes."""
    if not isinstance(live, dict):
        return live
    verdict = live.get("verdict")
    if not isinstance(verdict, dict) or verdict.get("ok") is not True:
        return live
    probed = _parse_ts(live.get("probed_at"))
    age = None
    if probed is not None:
        age = (datetime.now(timezone.utc) - probed).total_seconds()
    shell_stale = live.get("stale") is True
    if not shell_stale and (age is None or age <= LIVE_STALE_AFTER_S):
        return live
    out = dict(live)
    v = dict(verdict)
    v["last_attested"] = {"ok": True,
                          "reason": verdict.get("reason"),
                          "probed_at": live.get("probed_at")}
    v["ok"] = None
    v["state"] = "indeterminate"
    v["reason"] = ("stale attestation: last probe %s (%s); no fresh "
                   "probe within the %ds cadence window, so the "
                   "stored success is not asserted"
                   % (live.get("probed_at"),
                      ("%ds old" % int(age)) if age is not None
                      else "age unknown", LIVE_STALE_AFTER_S))
    out["verdict"] = v
    out["decayed"] = True
    return out


def service_live(svc):
    st = launchd_status(svc.get("launchd_label", ""))
    hp = health_probe(svc.get("health"))
    if st.get("running"):
        state = "running"
    elif st.get("loaded"):
        state = "loaded_idle"  # e.g. interval jobs between runs
    elif st.get("loaded") is False:
        state = "not_loaded"
    else:
        state = "unknown"
    stopped_as_desired = svc.get("desired_state") == "stopped"
    out = {"id": svc.get("id"), "launchd_label": svc.get("launchd_label"),
           "kind": svc.get("kind"), "repo": svc.get("repo"),
           "desired_state": svc.get("desired_state"),
           "stopped_as_desired": stopped_as_desired,
           "actions": svc.get("actions", []),
           "state": state, "pid": st.get("pid"),
           "health": hp}
    # Shell release (app 1.6.0): the registry service may carry the
    # shell's attested `live` block (payload probe-runner verdict +
    # launchd state + probed_at, folded in by the app and synced to
    # Postgres for the monitor's R1 rules). Surface it beside this server's
    # own read-time probe so callers can use the attested state
    # without a second host contact — decayed per _decay_live_block
    # when its attestation is older than the probe cadence, so a
    # stored success is never asserted indefinitely (v1.10.0).
    if isinstance(svc.get("live"), dict):
        out["registry_live"] = _decay_live_block(svc["live"])
    return out


def registry_services(reg):
    return reg.get("services", []) if reg else []


def tool_services(args):
    reg = load_registry()
    if reg is None:
        return {"ok": False, "error": "registry missing or unreadable at " + REGISTRY}
    svcs = registry_services(reg)
    want = args.get("service_id")
    if want:
        svcs = [s for s in svcs if s.get("id") == want]
    return {"ok": True, "host": reg.get("host", HOST_LABEL),
            "generated_at": reg.get("generated_at"),
            "count": len(svcs),
            "services": [service_live(s) for s in svcs]}


def tool_service_invoke(args):
    service_id = args.get("service_id") or ""
    action = args.get("action") or ""
    reg = load_registry()
    if reg is None:
        return {"ok": False, "error": "registry missing or unreadable at " + REGISTRY}
    svc = None
    for s in registry_services(reg):
        if s.get("id") == service_id:
            svc = s
            break
    if svc is None:
        return {"ok": False, "error": "unknown service_id: " + service_id}
    if action not in ("status", "start", "stop", "restart", "logs_recent", "update"):
        return {"ok": False, "error": "unknown action: " + action}
    if action not in svc.get("actions", []):
        return {"ok": False, "error": ("action %s not registered for service %s "
                                        "(registered: %s)" % (
                                            action, service_id,
                                            ", ".join(svc.get("actions", []))))}
    params = {"service_id": service_id}
    if action == "update":
        params["expected_sha256"] = args.get("expected_sha256")
        params["expected_commit"] = args.get("expected_commit")
    result, req_id = app_call({"op": "service_" + action,
                               "params": params}, 210 if action == "update" else 60)
    if result is None:
        return {"ok": False, "error_kind": "app_not_answering",
                "error": ("Factotum.app did not answer service %s %s; "
                          "service management dispatches through the app" % (
                              action, service_id)),
                "request_id": req_id, "service": svc}
    result.setdefault("invoked", True)
    result["service"] = svc
    return result


def _boot_flag(volume):
    """A registry volume's boot flag. The shell writes a JSON
    bool; a numeric 1 from any other writer counts too."""
    b = (volume or {}).get("boot")
    return b is True or b == 1


def disk_rollup(reg):
    """Disk state from the registry's disk section (written by
    Factotum.app; see the factotum repo's registry-schema doc). The
    state is recomputed here from the section's own numbers and
    stated thresholds, so a stale app-side verdict cannot lie:
    warn when the driving volume's free < warn_free_pct (default
    15), critical when its free < critical_free_pct (default 8)
    or a watched path exceeds its cap. Returns None when the
    registry predates the disk section (pre-1.3.0).

    Which volumes drive the state mirrors the shell's
    computeDiskState (boot-anchored since shell 1.6.4, scan #3
    item 5): when any volume carries the boot flag, only the
    boot volume's free space drives warn/critical; non-boot
    volumes stay in the returned per-volume detail but never
    move the headline (a Time Machine destination sits
    near-full by design, and under the worst-volume rule the
    Host1 headline read "critical" permanently off its TM
    volume while its boot volume sat at ~20% free). When no
    volume is flagged boot (registries predating the flag),
    fall back to the worst-volume rule over all volumes so
    the headline never goes blind."""
    disk = (reg or {}).get("disk")
    if not isinstance(disk, dict):
        return None
    th = disk.get("thresholds") or {}
    warn_pct = th.get("warn_free_pct", 15)
    crit_pct = th.get("critical_free_pct", 8)
    caps = th.get("watched_caps_bytes") or {}
    raw_volumes = disk.get("volumes") or []
    any_boot = any(_boot_flag(v) for v in raw_volumes)
    volumes = []
    state = "ok"
    reasons = []
    for v in raw_volumes:
        total = v.get("total_bytes") or 0
        free = v.get("free_bytes") or 0
        free_pct = round(100.0 * free / total, 2) if total else None
        row = dict(v)
        row["free_pct"] = free_pct
        volumes.append(row)
        if free_pct is None:
            continue
        if any_boot and not _boot_flag(v):
            continue
        if free_pct < crit_pct:
            state = "critical"
            reasons.append("%s free %.1f%% < critical %.0f%%"
                           % (v.get("mount"), free_pct, crit_pct))
        elif free_pct < warn_pct:
            if state == "ok":
                state = "warn"
            reasons.append("%s free %.1f%% < warn %.0f%%"
                           % (v.get("mount"), free_pct, warn_pct))
    watched = []
    for w in disk.get("watched") or []:
        row = dict(w)
        cap = caps.get(w.get("id"))
        if cap is not None:
            row.setdefault("cap_bytes", cap)
        over = cap is not None and (w.get("bytes") or 0) > cap
        row["over_cap"] = over
        watched.append(row)
        if over:
            state = "critical"
            reasons.append("watched path %s is %d bytes, over its "
                           "%d-byte cap" % (w.get("id"),
                                            w.get("bytes") or 0, cap))
    return {"state": state, "reasons": reasons,
            "measured_at": disk.get("measured_at"),
            "volumes": volumes, "watched": watched,
            "thresholds": {"warn_free_pct": warn_pct,
                           "critical_free_pct": crit_pct,
                           "watched_caps_bytes": caps},
            "last_cleanup": disk.get("last_cleanup")}


def packages_rollup(reg):
    """Packages state from the registry's packages section (written
    by factotum-pkg via Factotum.app; see the factotum repo's
    registry-schema doc).

    v1.5.0 semantics: packages_state keys off ACTIONABLE outdated
    only — items in the auto class, the ones the weekly apply will
    actually execute. Report-only classes (held, held-major,
    pinned, self-updating, needs-manual) are counted separately by
    class with their oldest first_seen age, and expired exception
    records surface as exceptions_due: they never turn the state
    red, but they are never silent either. States: ok (nothing
    actionable), outdated (actionable work waiting), stale (the
    section's measured_at is older than 36h — factotum-pkg
    refreshes daily, so a stale section means its agent is not
    running), unknown (section present but no inventory collected
    yet). Returns None when the registry predates the packages
    section (pre-1.4.0)."""
    pkg = (reg or {}).get("packages")
    if not isinstance(pkg, dict):
        return None
    counts = pkg.get("counts") or {}
    measured = pkg.get("measured_at")
    age_s = None
    if measured:
        ts = _parse_ts(measured)
        if ts is not None:
            age_s = int((datetime.now(timezone.utc) - ts).total_seconds())
    outdated_block = pkg.get("outdated") or {}
    all_items = []
    for group in outdated_block.values():
        if isinstance(group, list):
            all_items.extend(group)
    actionable = sum(1 for p in all_items if p.get("cls") == "auto")
    if not all_items and counts.get("outdated_auto") is not None:
        actionable = counts.get("outdated_auto") or 0
    report_only_by_class = {}
    oldest_report_age = None
    exceptions_due = 0
    for p in all_items:
        cls = p.get("cls") or "auto"
        if cls != "auto":
            report_only_by_class[cls] = \
                report_only_by_class.get(cls, 0) + 1
            age = p.get("age_days")
            if isinstance(age, (int, float)):
                oldest_report_age = age if oldest_report_age is None \
                    else max(oldest_report_age, age)
        if p.get("exception_due"):
            exceptions_due += 1
    policy = pkg.get("policy") or {}
    if policy.get("exceptions_due"):
        exceptions_due = max(exceptions_due, policy["exceptions_due"])
    if not measured:
        state = "unknown"
    elif age_s is not None and age_s > 36 * 3600:
        state = "stale"
    elif actionable:
        state = "outdated"
    else:
        state = "ok"
    return {"state": state, "measured_at": measured, "age_s": age_s,
            "counts": counts, "managers": pkg.get("managers"),
            "policy": pkg.get("policy"), "schedule": pkg.get("schedule"),
            "last_run": pkg.get("last_run"),
            "errors": pkg.get("errors"),
            "actionable_outdated": actionable,
            "report_only_by_class": report_only_by_class,
            "report_only_count": sum(report_only_by_class.values()),
            "oldest_report_only_age_days": oldest_report_age,
            "exceptions_due": exceptions_due,
            "ghost_dropped": pkg.get("ghost_dropped") or []}


def tool_health(args):
    reg = load_registry()
    out = {"ok": True, "host": HOST_LABEL, "mcp": self_health(reg),
           "factotum_app": None, "services": [], "summary": {}}
    if reg is None:
        out["ok"] = False
        out["error"] = "registry missing or unreadable at " + REGISTRY
        return out
    gen = reg.get("generated_at")
    out["registry_generated_at"] = gen
    # Shell release (app 1.6.0) sections, passed through verbatim:
    # permissions (the shell is the truth source for grant state),
    # payload (version + state), and the inbox/outbox depths the monitor's
    # R1 rules consume. Absent on pre-1.6.0 registries.
    for key in ("permissions", "payload", "inbox_depth", "outbox_depth"):
        if key in reg:
            out[key] = reg[key]
    live = [service_live(s) for s in registry_services(reg)]
    out["services"] = live
    counts = {}
    for s in live:
        counts[s["state"]] = counts.get(s["state"], 0) + 1
        if s["id"] == "factotum":
            out["factotum_app"] = s
    # v1.5.0: services whose desired_state is "stopped" are
    # "stopped as desired" — they leave the health denominator
    # entirely (a retired service that is correctly not running
    # must not count against the host; the retired PhotoPull pair
    # did, through 1.4.0).
    stopped = [s for s in live if s.get("stopped_as_desired")]
    checked_pool = [s for s in live if not s.get("stopped_as_desired")]
    healthy = sum(1 for s in checked_pool if s["health"].get("ok"))
    # v1.9.0: not_yet_run jobs (loaded, no recorded run since
    # agent load, evidence absent) are neutral — ok=None already keeps
    # them out of health_checked, and they get their own tally so
    # the state is visible rather than silently uncounted.
    not_yet_run = [s for s in checked_pool
                   if (s.get("health") or {}).get("state")
                   == "not_yet_run"]
    out["summary"] = {"services_total": len(live), "by_state": counts,
                      "health_ok": healthy,
                      "health_checked": sum(1 for s in checked_pool
                                            if s["health"].get("ok") is not None),
                      "stopped_as_desired": len(stopped),
                      "stopped_as_desired_ids": [s["id"] for s in stopped],
                      "not_yet_run": len(not_yet_run),
                      "not_yet_run_ids": [s["id"] for s in not_yet_run]}
    disk = disk_rollup(reg)
    out["disk"] = disk
    out["summary"]["disk_state"] = disk["state"] if disk else "unknown"
    packages = packages_rollup(reg)
    out["packages"] = packages
    out["summary"]["packages_state"] = packages["state"] if packages \
        else "unknown"
    if packages:
        # v1.5.0: packages_outdated is the ACTIONABLE count (auto
        # class). Report-only items are their own numbers so a host
        # full of held exceptions never reads as work waiting.
        out["summary"]["packages_outdated"] = \
            packages.get("actionable_outdated", 0)
        out["summary"]["packages_report_only"] = \
            packages.get("report_only_count", 0)
        out["summary"]["packages_exceptions_due"] = \
            packages.get("exceptions_due", 0)
    # v1.10.0 riders: the outbox depth the monitor's rules watch,
    # promoted into the summary with a backlog flag (>500 means
    # the reaper or the app is behind), and a packages staleness
    # flag at 26h — a full day earlier than the packages
    # section's own 36h stale state, so the warning lands with
    # margin. Depths ride the registry top level (shell-written).
    out["summary"]["outbox_depth"] = reg.get("outbox_depth")
    out["summary"]["outbox_backlog"] = bool(
        isinstance(reg.get("outbox_depth"), int)
        and reg.get("outbox_depth") > 500)
    if packages:
        p_age = packages.get("age_s")
        out["summary"]["packages_age_s"] = p_age
        out["summary"]["packages_stale"] = bool(
            p_age is not None and p_age > 26 * 3600)
    return out


# --- Postgres registry sync ---------------------------------------------------
#
# This host is authoritative for its own registry.json; Postgres Postgres
# (db postgres on host1, factotum_* tables, DDL in postgres-schema.sql)
# holds a replica per host for cross-host visibility and parity. A host
# writes ONLY its own rows (host key = HOST_LABEL, the FACTOTUM_HOST
# value from this service's .env). psycopg is imported lazily here so
# server startup stays stdlib-only, and every failure is captured into
# SYNC_STATE and returned as data: sync problems never take the MCP
# down.

SYNC_STATE = {"last_attempt": None, "last_ok": None, "last_reason": None,
              "last_error": None, "last_hash": None}
SYNC_LOCK = threading.Lock()
EXPECTED_HOSTS = ("host2", "host1")


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_hash(reg):
    blob = json.dumps(reg, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _parse_ts(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _pg_connect():
    """Lazy psycopg connect. Raises RuntimeError with a plain message
    when sync is not configured or the driver is unavailable."""
    if not DB_DSN:
        raise RuntimeError("FACTOTUM_DB_DSN is not set in this host's "
                           "factotum-mcp .env; registry sync is not "
                           "configured on this host")
    try:
        import psycopg
    except ImportError:
        raise RuntimeError("psycopg is not installed in this service's "
                           "python environment; registry sync unavailable")
    return psycopg.connect(DB_DSN, connect_timeout=8)


def _config_flag(name, default=True):
    """Read a boolean from the Factotum app's config.json (the
    Settings window writes it). Unreadable/missing -> default."""
    try:
        with open(CONFIG) as f:
            cfg = json.load(f)
        v = cfg.get(name, default)
        return v if isinstance(v, bool) else default
    except Exception:
        return default


def _record_sync(ok, reason, error=None, content_hash=None):
    with SYNC_LOCK:
        SYNC_STATE["last_attempt"] = _utcnow_iso()
        SYNC_STATE["last_ok"] = ok
        SYNC_STATE["last_reason"] = reason
        SYNC_STATE["last_error"] = error
        if content_hash:
            SYNC_STATE["last_hash"] = content_hash
        state = dict(SYNC_STATE)
    # Mirror the outcome to sync-status.json for the app's UI (it
    # holds no secret and never speaks HTTP, so a file in the shared
    # support dir is its only legitimate window into sync state).
    try:
        reg = load_registry() or {}
        status = {
            "host": HOST_LABEL.lower(),
            "last_attempt_at": state["last_attempt"],
            "last_ok": state["last_ok"],
            "last_reason": state["last_reason"],
            "last_error": state["last_error"],
            "content_hash": state["last_hash"],
            "registry_generated_at": reg.get("generated_at"),
            "sync_enabled": _config_flag("postgres_sync_enabled", True),
        }
        tmp = SYNC_STATUS + ".tmp"
        with open(tmp, "w") as f:
            json.dump(status, f, indent=1, sort_keys=True)
        os.replace(tmp, SYNC_STATUS)
    except Exception:
        pass  # the status file is a display aid; never fail a sync over it


def _packages_rows(host, reg):
    """Flatten the registry's packages section (v1.4.0) into
    factotum_packages rows: (host, manager, name, kind, version,
    available_version, class, outdated, coverage). Inventory rows
    carry the installed version; outdated rows merge in the
    available version and the policy class from the section's
    classified outdated lists. v1.5.0 adds: dependency formulae
    (kind=dependency), standalone manager binaries (manager=self,
    kind=self-managed), and the per-row coverage state
    (inventoried / unversioned / refused). Pre-1.4.0 registries
    (no section) yield no rows."""
    pkg = (reg or {}).get("packages")
    if not isinstance(pkg, dict):
        return []
    inv = pkg.get("inventory") or {}
    outd = pkg.get("outdated") or {}
    rows = []

    def outdated_map(items, key="name"):
        return {i.get(key): i for i in items or [] if i.get(key)}

    def coverage_of(item):
        return item.get("coverage") or (
            "inventoried" if item.get("version") else "unversioned")

    f_out = outdated_map(outd.get("formulae"))
    for f in inv.get("formulae") or []:
        o = f_out.get(f.get("name"))
        rows.append((host, "brew", f.get("name"), "formula",
                     f.get("version"),
                     o.get("available") if o else None,
                     o.get("cls") if o else None, bool(o),
                     coverage_of(f)))
    for d in inv.get("formulae_dependencies") or []:
        o = f_out.get(d.get("name"))
        rows.append((host, "brew", d.get("name"), "dependency",
                     d.get("version"),
                     o.get("available") if o else None,
                     o.get("cls") if o else None, bool(o),
                     coverage_of(d)))
    c_out = outdated_map(outd.get("casks"))
    for c in inv.get("casks") or []:
        o = c_out.get(c.get("token"))
        rows.append((host, "brew", c.get("token"), "cask",
                     c.get("version"),
                     o.get("available") if o else None,
                     o.get("cls") if o else c.get("class"), bool(o),
                     coverage_of(c)))
    n_out = outdated_map(outd.get("npm"))
    for n in inv.get("npm") or []:
        o = n_out.get(n.get("name"))
        rows.append((host, "npm", n.get("name"), "npm-global",
                     n.get("version"),
                     o.get("available") if o else None,
                     o.get("cls") if o else None, bool(o),
                     coverage_of(n)))
    for p in inv.get("pipx") or []:
        rows.append((host, "pipx", p.get("name"), "pipx",
                     p.get("version"), None, None, False,
                     coverage_of(p)))
    for u in inv.get("uv_tools") or []:
        rows.append((host, "uv", u.get("name"), "uv-tool",
                     u.get("version"), None, None, False,
                     coverage_of(u)))
    m_out = outdated_map(outd.get("mas"))
    for m in inv.get("mas") or []:
        o = m_out.get(m.get("name"))
        rows.append((host, "mas", m.get("name"), "app-store",
                     m.get("version"),
                     o.get("available") if o else None,
                     o.get("cls") if o else None, bool(o),
                     coverage_of(m)))
    for sm in inv.get("self_managed") or []:
        rows.append((host, "self", sm.get("name"), "self-managed",
                     sm.get("version"), sm.get("available"),
                     "report-only" if sm.get("outdated") else None,
                     bool(sm.get("outdated")), coverage_of(sm)))
    return [r for r in rows if r[2]]


_PACKAGES_HAS_COVERAGE = None


def _packages_coverage_column(cur):
    """Whether factotum_packages has the v1.5.0 coverage column
    (probed once per process; a failed probe degrades to the
    pre-1.5.0 column set rather than failing the sync)."""
    global _PACKAGES_HAS_COVERAGE
    if _PACKAGES_HAS_COVERAGE is not None:
        return _PACKAGES_HAS_COVERAGE
    try:
        cur.execute("SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'factotum_packages' "
                    "AND column_name = 'coverage'")
        _PACKAGES_HAS_COVERAGE = cur.fetchone() is not None
    except Exception:
        _PACKAGES_HAS_COVERAGE = False
    return _PACKAGES_HAS_COVERAGE


_CANARY_OUTCOME_LIMIT = 200
_CANARY_DETAIL_LIMIT = 2000
def _canary_record(value):
    """A bounded, complete record; missing/invalid timestamps cannot erase history."""
    if not isinstance(value, dict):
        return None
    if (not isinstance(value.get("last_exercise_outcome"), str) or
            not isinstance(value.get("last_exercise_detail"), str)):
        return None
    when, bad = _canary_ts(value.get("last_exercised_at"))
    if bad or when is None:
        return None
    return {
        "last_exercised_at": when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "last_exercise_outcome": _canary_text(value.get("last_exercise_outcome"),
                                                _CANARY_OUTCOME_LIMIT),
        "last_exercise_detail": _canary_text(value.get("last_exercise_detail"),
                                               _CANARY_DETAIL_LIMIT),
    }


def _canary_judged_invalid(value):
    """A producer judgment, not an exercise record: outcome and
    detail are present as text but the timestamp is missing or
    unparseable. Shell 1.6.11 (internal ticket) emits exactly this shape
    when it folds a record whose last_exercised_at is missing or
    malformed to 'indeterminate' and drops the stamp. The judgment
    is authoritative: stored history must not resurrect the older
    exercise it replaced (internal ticket). A value missing outcome or
    detail text is not a judgment; it is fieldless input, and
    internal ticket history preservation still applies to it."""
    if not isinstance(value, dict):
        return False
    if (not isinstance(value.get("last_exercise_outcome"), str) or
            not isinstance(value.get("last_exercise_detail"), str)):
        return False
    when, bad = _canary_ts(value.get("last_exercised_at"))
    return bad or when is None


def _load_canary_state():
    """Read only this host's state file; never mix state from another host."""
    path = os.path.join(SUPPORT, "canary-state.json")
    if not os.path.exists(path):
        return {}, 0
    value = read_json(path)
    actions = value.get("actions") if isinstance(value, dict) else None
    if not isinstance(actions, dict):
        return {}, 1
    valid = {}
    malformed = 0
    for action_id, record in actions.items():
        if isinstance(action_id, str) and _canary_record(record):
            valid[action_id] = record
        else:
            malformed += 1
    return valid, malformed


def _overlay_canary_records(reg, source):
    """Keep the newest valid exercise for each action still in this host's registry."""
    actions = reg.get("actions")
    if not isinstance(actions, list) or not isinstance(source, dict):
        return reg, 0
    changed = 0
    merged = []
    for action in actions:
        if not isinstance(action, dict) or not isinstance(action.get("id"), str):
            merged.append(action)
            continue
        current = _canary_record(action)
        candidate = _canary_record(source.get(action["id"]))
        if current is None and _canary_judged_invalid(action):
            # The producer judged this action's record invalid
            # (internal ticket): history does not override a judgment.
            merged.append(action)
            continue
        if candidate and (not current or
                          _canary_ts(candidate["last_exercised_at"])[0] >
                          _canary_ts(current["last_exercised_at"])[0]):
            action = dict(action)
            action.update(candidate)
            changed += 1
        merged.append(action)
    if not changed:
        return reg, 0
    out = dict(reg)
    out["actions"] = merged
    return out, changed


def _actions_canary_columns(cur):
    """Whether public.factotum_actions has the canary columns
    (a Postgres schema migration) with the expected types.
    Probed on every sync and never cached in either direction:
    a server that started before the migration landed picks
    the columns up on its next sync without a restart, and if
    the columns are dropped again (the migration's documented
    rollback) a running server degrades to the pre-1.11.0
    column set instead of failing every sync until a restart.
    The probe is pinned to the public schema and checks types,
    so a same-named table in another schema cannot corrupt the
    answer. A failed probe degrades to the pre-1.11.0 column
    set rather than failing the sync."""
    try:
        cur.execute("SELECT column_name, data_type FROM "
                    "information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND table_name = 'factotum_actions' "
                    "AND column_name IN ('last_exercised_at', "
                    "'last_exercise_outcome', 'last_exercise_detail')")
        found = {r[0]: r[1] for r in cur.fetchall()}
    except Exception:
        return False
    return found == {"last_exercised_at": "timestamp with time zone",
                     "last_exercise_outcome": "text",
                     "last_exercise_detail": "text"}


def _canary_text(value, limit):
    """Coerce a registry canary text field for a Postgres text
    column. Non-strings drop to None; NUL and the other C0
    control characters are stripped (Postgres text cannot store
    them, and one poisoned value must not fail the whole sync),
    except tab/newline/carriage return, which tool output
    legitimately contains; the value is capped at limit so a
    runaway detail cannot bloat the row."""
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in value
                      if ch in "\t\n\r" or ord(ch) >= 0x20)
    return cleaned[:limit]


def _canary_ts(value):
    """Parse a canary last_exercised_at. The runner contract is
    an ISO-8601 Z string, but a numeric epoch (a Swift runner
    writing timeIntervalSince1970 lands here as int/float
    through JSON) is also accepted. Returns (datetime|None,
    present_but_unparseable)."""
    if isinstance(value, bool):
        return None, True
    if isinstance(value, (int, float)):
        try:
            return (datetime.fromtimestamp(value, tz=timezone.utc),
                    False)
        except (OverflowError, OSError, ValueError):
            return None, True
    parsed = _parse_ts(value)
    return parsed, bool(value) and parsed is None


def _action_rows_detailed(host, actions, with_canary):
    """_action_rows plus the ids of actions whose
    last_exercised_at was present but unparseable (those land
    as NULL and are named in the sync result's canary_note, so
    the loss is observable rather than silent)."""
    rows = []
    bad_ts = []
    for a in actions:
        if not a.get("id"):
            continue
        row = (host, a.get("id"), a.get("app_bundle_id"),
               a.get("name"), a.get("op"), a.get("mechanism"),
               a.get("risk"), a.get("status"))
        if with_canary:
            parsed_ts, unparseable = _canary_ts(
                a.get("last_exercised_at"))
            if unparseable:
                bad_ts.append(a.get("id"))
            row += (parsed_ts,
                    _canary_text(a.get("last_exercise_outcome"),
                                 _CANARY_OUTCOME_LIMIT),
                    _canary_text(a.get("last_exercise_detail"),
                                 _CANARY_DETAIL_LIMIT))
        rows.append(row)
    return rows, bad_ts


def _action_rows(host, actions, with_canary):
    """Normalized factotum_actions rows from registry action
    dicts. With the canary columns present, each row also
    carries the canary exercise fields from the registry
    action: last_exercised_at parsed to a timestamp, outcome
    and detail coerced to safe text; actions never exercised
    carry NULLs. Without them, the pre-1.11.0 8-column rows,
    unchanged."""
    return _action_rows_detailed(host, actions, with_canary)[0]


def sync_push(reason="manual"):
    """Sync this host's registry.json into Postgres. Returns a result
    dict; never raises. One transaction: snapshot upsert + normalized
    rows for THIS host replaced. The content hash short-circuits an
    unchanged registry to a bare synced_at touch."""
    if not _config_flag("postgres_sync_enabled", True):
        msg = ("registry sync is disabled in Factotum Settings "
               "(config.json postgres_sync_enabled=false)")
        out = {"ok": True, "host": HOST_LABEL.lower(),
               "skipped": "sync_disabled", "note": msg}
        _record_sync(False, reason, msg)
        return out
    reg = load_registry()
    if reg is None:
        out = {"ok": False, "error": "registry missing or unreadable at " + REGISTRY}
        _record_sync(False, reason, out["error"])
        return out
    state, malformed_state = _load_canary_state()
    reg = assembled_registry(reg, canary_state=state)
    host = HOST_LABEL.lower()
    action_ids = {a.get("id") for a in (reg.get("actions") or [])
                  if isinstance(a, dict) and isinstance(a.get("id"), str)}
    pre_canary_notes = []
    ignored_state = malformed_state + len(set(state) - action_ids)
    if ignored_state:
        pre_canary_notes.append("ignored %d malformed or unknown canary state record(s)" % ignored_state)
    chash = canonical_hash(reg)
    try:
        from psycopg.types.json import Jsonb
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT content_hash, registry FROM factotum_registries "
                            "WHERE host = %s FOR UPDATE", (host,))
                row = cur.fetchone()
                # Reconcile under the same transaction as the blob and row
                # writes. The incoming producer may be an older shell without
                # canary keys; absence is not an audited request to clear.
                stored = row[1] if row and len(row) > 1 and isinstance(row[1], dict) else {}
                stored_by_id = {a["id"]: a for a in (stored.get("actions") or [])
                                if isinstance(a, dict) and isinstance(a.get("id"), str)}
                try:
                    cur.execute("SAVEPOINT factotum_canary_history")
                    if _actions_canary_columns(cur):
                        cur.execute("SELECT action_id, last_exercised_at, "
                                    "last_exercise_outcome, last_exercise_detail "
                                    "FROM public.factotum_actions WHERE host = %s", (host,))
                        for aid, when, outcome, detail in cur.fetchall():
                            candidate = {"last_exercised_at": when,
                                         "last_exercise_outcome": outcome,
                                         "last_exercise_detail": detail}
                            prior = _canary_record(stored_by_id.get(aid))
                            current = _canary_record(candidate)
                            if current and (not prior or
                                            _canary_ts(current["last_exercised_at"])[0] >
                                            _canary_ts(prior["last_exercised_at"])[0]):
                                stored_by_id[aid] = candidate
                    cur.execute("RELEASE SAVEPOINT factotum_canary_history")
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT factotum_canary_history")
                    cur.execute("RELEASE SAVEPOINT factotum_canary_history")
                    # A failed read cannot justify replacing history with
                    # NULLs. Abort the entire transaction before any write.
                    raise RuntimeError("Postgres canary history probe unavailable")
                reg, preserved = _overlay_canary_records(reg, stored_by_id)
                if preserved:
                    pre_canary_notes.append("preserved %d newer Postgres canary record(s)" % preserved)
                chash = canonical_hash(reg)
                if row and row[0] == chash:
                    # Registry unchanged: refresh synced_at, and
                    # (v1.11.0) backfill the canary columns in
                    # place when they exist. Without this, a host
                    # whose registry has not changed since before
                    # migration 2365 keeps NULL canary values
                    # until some unrelated registry change forces
                    # a full replace (adversarial review blocker,
                    # internal ticket). Both blocks run behind savepoints
                    # and degrade to notes: a canary problem must
                    # never fail the sync.
                    canary_cols = False
                    canary_backfilled = 0
                    canary_notes = list(pre_canary_notes)
                    try:
                        cur.execute("SAVEPOINT factotum_canary_probe")
                        canary_cols = _actions_canary_columns(cur)
                        cur.execute("RELEASE SAVEPOINT "
                                    "factotum_canary_probe")
                    except Exception:
                        try:
                            cur.execute("ROLLBACK TO SAVEPOINT "
                                        "factotum_canary_probe")
                            cur.execute("RELEASE SAVEPOINT "
                                        "factotum_canary_probe")
                        except Exception:
                            pass
                        canary_cols = False
                    if canary_cols:
                        try:
                            cur.execute("SAVEPOINT "
                                        "factotum_canary_backfill")
                            want, bad_ts = _action_rows_detailed(
                                host, reg.get("actions", []), True)
                            if bad_ts:
                                canary_notes.append(
                                    "unparseable last_exercised_at "
                                    "stored as NULL for: "
                                    + ", ".join(bad_ts))
                            cur.execute(
                                "SELECT action_id, "
                                "last_exercised_at, "
                                "last_exercise_outcome, "
                                "last_exercise_detail FROM "
                                "public.factotum_actions "
                                "WHERE host = %s", (host,))
                            have = {r[0]: r[1:]
                                    for r in cur.fetchall()}
                            for w in want:
                                if have.get(w[1]) != w[8:]:
                                    cur.execute(
                                        "UPDATE "
                                        "public.factotum_actions "
                                        "SET last_exercised_at = %s, "
                                        "last_exercise_outcome = %s, "
                                        "last_exercise_detail = %s "
                                        "WHERE host = %s "
                                        "AND action_id = %s",
                                        (w[8], w[9], w[10], host,
                                         w[1]))
                                    canary_backfilled += 1
                            cur.execute("RELEASE SAVEPOINT "
                                        "factotum_canary_backfill")
                        except Exception as be:
                            try:
                                cur.execute("ROLLBACK TO SAVEPOINT "
                                            "factotum_canary_backfill")
                                cur.execute("RELEASE SAVEPOINT "
                                            "factotum_canary_backfill")
                            except Exception:
                                pass
                            canary_notes.append(
                                "canary backfill skipped: %s" % be)
                    cur.execute("UPDATE factotum_registries "
                                "SET synced_at = now() WHERE host = %s",
                                (host,))
                    conn.commit()
                    out = {"ok": True, "host": host, "changed": False,
                           "content_hash": chash,
                           "generated_at": reg.get("generated_at"),
                           "canary_columns": canary_cols,
                           "note": "registry unchanged; synced_at refreshed"}
                    if canary_backfilled:
                        out["canary_backfilled"] = canary_backfilled
                    if canary_notes:
                        out["canary_note"] = "; ".join(canary_notes)
                    _record_sync(True, reason, content_hash=chash)
                    return out
                cur.execute(
                    "INSERT INTO factotum_registries "
                    "(host, generated_at, synced_at, content_hash, registry) "
                    "VALUES (%s, %s, now(), %s, %s) "
                    "ON CONFLICT (host) DO UPDATE SET "
                    "generated_at = EXCLUDED.generated_at, "
                    "synced_at = now(), "
                    "content_hash = EXCLUDED.content_hash, "
                    "registry = EXCLUDED.registry",
                    (host, _parse_ts(reg.get("generated_at")), chash,
                     Jsonb(reg)))
                cur.execute("DELETE FROM factotum_apps WHERE host = %s",
                            (host,))
                cur.execute("DELETE FROM factotum_actions WHERE host = %s",
                            (host,))
                cur.execute("DELETE FROM factotum_services WHERE host = %s",
                            (host,))
                apps = [(host, a.get("bundle_id"), a.get("name"),
                         a.get("version"), a.get("installed"),
                         a.get("running"))
                        for a in reg.get("apps", []) if a.get("bundle_id")]
                # Canary fields (v1.11.0, internal ticket): registry
                # actions may carry last_exercised_at /
                # last_exercise_outcome / last_exercise_detail
                # (runner-written, shell-preserved). Migration
                # 2365 gave factotum_actions columns for them;
                # the rows carry them whenever the columns are
                # present. The probe runs behind a savepoint, the
                # same containment the packages block below uses:
                # a failed probe query would otherwise leave the
                # transaction aborted even with the exception
                # caught, and a probe failure must never fail the
                # sync — it degrades to the old column set.
                try:
                    cur.execute("SAVEPOINT factotum_canary_probe")
                    canary_cols = _actions_canary_columns(cur)
                    cur.execute("RELEASE SAVEPOINT "
                                "factotum_canary_probe")
                except Exception:
                    try:
                        cur.execute("ROLLBACK TO SAVEPOINT "
                                    "factotum_canary_probe")
                        cur.execute("RELEASE SAVEPOINT "
                                    "factotum_canary_probe")
                    except Exception:
                        pass
                    canary_cols = False
                actions, bad_ts = _action_rows_detailed(
                    host, reg.get("actions", []), canary_cols)
                canary_notes = list(pre_canary_notes)
                if bad_ts:
                    canary_notes.append(
                        "unparseable last_exercised_at stored as "
                        "NULL for: " + ", ".join(bad_ts))
                services = [(host, s.get("id"), s.get("launchd_label"),
                             s.get("kind"), s.get("desired_state"))
                            for s in reg.get("services", [])
                            if s.get("id")]
                cur.executemany(
                    "INSERT INTO factotum_apps "
                    "(host, bundle_id, name, version, installed, running) "
                    "VALUES (%s, %s, %s, %s, %s, %s)", apps)
                # The actions insert sits behind its own
                # savepoint: if the canary column set fails for
                # any reason the probe did not foresee, the sync
                # degrades to the legacy 8-column rows (with a
                # canary_note) instead of aborting the whole
                # host sync.
                try:
                    cur.execute("SAVEPOINT factotum_actions")
                    if canary_cols:
                        cur.executemany(
                            "INSERT INTO public.factotum_actions "
                            "(host, action_id, app_bundle_id, name, op, "
                            "mechanism, risk, status, last_exercised_at, "
                            "last_exercise_outcome, last_exercise_detail) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
                            "%s, %s)", actions)
                    else:
                        cur.executemany(
                            "INSERT INTO public.factotum_actions "
                            "(host, action_id, app_bundle_id, name, op, "
                            "mechanism, risk, status) VALUES "
                            "(%s, %s, %s, %s, %s, %s, %s, %s)",
                            actions)
                    cur.execute("RELEASE SAVEPOINT factotum_actions")
                except Exception as ae:
                    try:
                        cur.execute("ROLLBACK TO SAVEPOINT "
                                    "factotum_actions")
                        cur.execute("RELEASE SAVEPOINT "
                                    "factotum_actions")
                    except Exception:
                        pass
                    if not canary_cols:
                        raise
                    actions = _action_rows(
                        host, reg.get("actions", []), False)
                    cur.executemany(
                        "INSERT INTO public.factotum_actions "
                        "(host, action_id, app_bundle_id, name, op, "
                        "mechanism, risk, status) VALUES "
                        "(%s, %s, %s, %s, %s, %s, %s, %s)", actions)
                    canary_notes.append(
                        "canary insert failed (%s); legacy 8-column "
                        "rows written" % ae)
                cur.executemany(
                    "INSERT INTO factotum_services "
                    "(host, service_id, launchd_label, kind, desired_state) "
                    "VALUES (%s, %s, %s, %s, %s)", services)
                # Normalized package rows (v1.4.0), behind a
                # savepoint: if the factotum_packages table does not
                # exist yet (its DDL is in postgres-schema.sql, applied
                # ad hoc like the original four tables), the rest of
                # the sync must not fail — the registry blob carries
                # the packages section regardless, and the outcome is
                # reported in the result's packages_note.
                packages_rows = _packages_rows(host, reg)
                packages_note = None
                try:
                    cur.execute("SAVEPOINT factotum_packages")
                    cur.execute("DELETE FROM factotum_packages "
                                "WHERE host = %s", (host,))
                    if packages_rows:
                        if _packages_coverage_column(cur):
                            cur.executemany(
                                "INSERT INTO factotum_packages "
                                "(host, manager, name, kind, version, "
                                "available_version, class, outdated, "
                                "coverage) VALUES (%s, %s, %s, %s, "
                                "%s, %s, %s, %s, %s)", packages_rows)
                        else:
                            cur.executemany(
                                "INSERT INTO factotum_packages "
                                "(host, manager, name, kind, version, "
                                "available_version, class, outdated) "
                                "VALUES (%s, %s, %s, %s, %s, %s, "
                                "%s, %s)",
                                [r[:8] for r in packages_rows])
                    cur.execute("RELEASE SAVEPOINT factotum_packages")
                except Exception as pe:
                    cur.execute("ROLLBACK TO SAVEPOINT factotum_packages")
                    cur.execute("RELEASE SAVEPOINT factotum_packages")
                    packages_note = ("factotum_packages rows skipped: "
                                     "%s" % pe)
                conn.commit()
        out = {"ok": True, "host": host, "changed": True,
               "content_hash": chash,
               "generated_at": reg.get("generated_at"),
               "canary_columns": canary_cols,
               "counts": {"apps": len(apps), "actions": len(actions),
                          "services": len(services),
                          "packages": len(packages_rows)}}
        if packages_note:
            out["packages_note"] = packages_note
        if canary_notes:
            out["canary_note"] = "; ".join(canary_notes)
        _record_sync(True, reason, content_hash=chash)
        return out
    except Exception as e:
        out = {"ok": False, "host": host,
               "error": "postgres sync failed: %s" % e}
        _record_sync(False, reason, out["error"], content_hash=chash)
        return out


def trigger_sync_async(reason):
    def _run():
        try:
            sync_push(reason)
        except Exception:
            pass  # sync_push records its own failures; belt and braces
    t = threading.Thread(target=_run, daemon=True,
                         name="factotum-registry-sync")
    t.start()
    return t


def _sync_request_watcher():
    """Consume app-dropped sync requests (Factotum's menu/window
    'Sync Now'). The request is consumed BEFORE the sync runs, so a
    slow or failed sync can never re-trigger itself; the outcome
    lands in SYNC_STATE + sync-status.json either way."""
    while True:
        try:
            os.makedirs(SYNC_REQUESTS_DIR, exist_ok=True)
            for name in sorted(os.listdir(SYNC_REQUESTS_DIR)):
                if not name.endswith(".json"):
                    continue
                path = os.path.join(SYNC_REQUESTS_DIR, name)
                try:
                    with open(path) as f:
                        req = json.load(f)
                except Exception:
                    req = {}
                try:
                    os.remove(path)
                except Exception:
                    pass
                reason = "app_request:%s" % str(req.get("reason", "menu"))[:48]
                sync_push(reason)
        except Exception:
            pass  # the watcher must never take the server down
        time.sleep(5)


def start_sync_request_watcher():
    t = threading.Thread(target=_sync_request_watcher, daemon=True,
                         name="factotum-sync-requests")
    t.start()
    return t


def postgres_snapshots():
    """All synced registry snapshots from Postgres, as plain dicts.
    Raises RuntimeError (plain message) when sync is not configured or
    Postgres is unreachable."""
    try:
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT host, generated_at, synced_at, "
                            "content_hash, registry FROM "
                            "factotum_registries ORDER BY host")
                rows = cur.fetchall()
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError("could not read synced registries from Postgres: "
                           "%s" % e)
    out = []
    for host, gen, synced, chash, registry in rows:
        out.append({"host": host,
                    "generated_at": gen.isoformat() if gen else None,
                    "synced_at": synced.isoformat() if synced else None,
                    "content_hash": chash, "registry": registry})
    return out


def tool_registry_sync(args):
    action = (args.get("action") or "").lower()
    if action == "push":
        return sync_push("tool")
    if action == "status":
        reg = load_registry()
        if reg is None:
            return {"ok": False,
                    "error": "registry missing or unreadable at " + REGISTRY}
        host = HOST_LABEL.lower()
        local = {"host": host, "generated_at": reg.get("generated_at"),
                 "content_hash": canonical_hash(assembled_registry(reg)),
                 "registry_host_field": reg.get("host")}
        out = {"ok": True, "configured": bool(DB_DSN), "local": local,
               "sync_runtime": dict(SYNC_STATE), "postgres": None,
               "peers": []}
        if not DB_DSN:
            out["ok"] = False
            out["error"] = ("FACTOTUM_DB_DSN is not set in this host's "
                            "factotum-mcp .env; registry sync is not "
                            "configured on this host")
            return out
        try:
            snaps = postgres_snapshots()
        except RuntimeError as e:
            out["ok"] = False
            out["error"] = str(e)
            return out
        for snap in snaps:
            meta = {"host": snap["host"],
                    "generated_at": snap["generated_at"],
                    "synced_at": snap["synced_at"],
                    "content_hash": snap["content_hash"]}
            if snap["host"] == host:
                meta["in_sync"] = (snap["content_hash"]
                                   == local["content_hash"])
                out["postgres"] = meta
            else:
                out["peers"].append(meta)
        if out["postgres"] is None:
            out["note"] = ("this host has not synced to Postgres yet; "
                           "run factotum_registry_sync push")
        return out
    return {"ok": False,
            "error": "action must be 'push' or 'status', got: %r"
                     % args.get("action")}


def tool_peer_registry(args):
    own = HOST_LABEL.lower()
    want = (args.get("host") or "").lower().removesuffix(".local")
    try:
        snaps = postgres_snapshots()
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    by_host = {s["host"]: s for s in snaps}
    if want:
        snap = by_host.get(want)
        if snap is None:
            return {"ok": False, "found": False, "host": want,
                    "note": ("host '%s' has not synced a registry to "
                             "Postgres yet (synced hosts: %s)"
                             % (want, ", ".join(sorted(by_host)) or "none"))}
        return {"ok": True, "host": snap["host"],
                "generated_at": snap["generated_at"],
                "synced_at": snap["synced_at"],
                "content_hash": snap["content_hash"],
                "is_own_host": snap["host"] == own,
                "registry": snap["registry"]}
    peers = [s for s in snaps if s["host"] != own]
    if not peers:
        return {"ok": False, "found": False,
                "note": ("no peer registry has synced to Postgres yet; "
                         "synced hosts: %s"
                         % (", ".join(sorted(by_host)) or "none"))}
    if len(peers) > 1:
        return {"ok": False, "found": False,
                "note": ("multiple peer registries are synced (%s); "
                         "pass host to pick one"
                         % ", ".join(s["host"] for s in peers))}
    snap = peers[0]
    return {"ok": True, "host": snap["host"],
            "generated_at": snap["generated_at"],
            "synced_at": snap["synced_at"],
            "content_hash": snap["content_hash"],
            "registry": snap["registry"]}


def _index_by(items, key):
    out = {}
    for item in items or []:
        k = item.get(key)
        if k:
            out[k] = item
    return out


def tool_parity(args):
    try:
        snaps = postgres_snapshots()
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    by_host = {s["host"]: s for s in snaps}
    present = sorted(by_host)
    if len(present) < 2:
        missing = [h for h in EXPECTED_HOSTS if h not in by_host]
        return {"ok": True, "parity_computable": False,
                "synced_hosts": [{"host": s["host"],
                                  "generated_at": s["generated_at"],
                                  "synced_at": s["synced_at"]}
                                 for s in snaps],
                "not_yet_synced": missing,
                "note": ("parity needs both hosts' registries in Postgres; "
                         "only %s has synced so far"
                         % (present[0] if present else "no host"))}
    own = HOST_LABEL.lower()
    a_host = own if own in by_host else present[0]
    b_host = [h for h in present if h != a_host][0]
    a, b = by_host[a_host], by_host[b_host]
    ra, rb = a["registry"] or {}, b["registry"] or {}

    a_apps, b_apps = _index_by(ra.get("apps"), "bundle_id"), \
        _index_by(rb.get("apps"), "bundle_id")
    apps_only_a = sorted(set(a_apps) - set(b_apps))
    apps_only_b = sorted(set(b_apps) - set(a_apps))

    a_act, b_act = _index_by(ra.get("actions"), "id"), \
        _index_by(rb.get("actions"), "id")
    act_only_a = sorted(set(a_act) - set(b_act))
    act_only_b = sorted(set(b_act) - set(a_act))
    act_diff = []
    for aid in sorted(set(a_act) & set(b_act)):
        fields = {}
        for f in ("op", "risk", "status"):
            if a_act[aid].get(f) != b_act[aid].get(f):
                fields[f] = {a_host: a_act[aid].get(f),
                             b_host: b_act[aid].get(f)}
        if fields:
            act_diff.append({"action_id": aid, "differences": fields})

    a_svc, b_svc = _index_by(ra.get("services"), "id"), \
        _index_by(rb.get("services"), "id")
    svc_only_a = sorted(set(a_svc) - set(b_svc))
    svc_only_b = sorted(set(b_svc) - set(a_svc))
    svc_diff = []
    for sid in sorted(set(a_svc) & set(b_svc)):
        if a_svc[sid].get("desired_state") != b_svc[sid].get("desired_state"):
            svc_diff.append({"service_id": sid,
                             "launchd_label": a_svc[sid].get("launchd_label"),
                             "desired_state": {
                                 a_host: a_svc[sid].get("desired_state"),
                                 b_host: b_svc[sid].get("desired_state")}})

    return {"ok": True, "parity_computable": True,
            "hosts": {a_host: {"generated_at": a["generated_at"],
                               "synced_at": a["synced_at"]},
                      b_host: {"generated_at": b["generated_at"],
                               "synced_at": b["synced_at"]}},
            "apps": {"only_on": {a_host: apps_only_a, b_host: apps_only_b},
                     "counts": {a_host: len(a_apps), b_host: len(b_apps)}},
            "actions": {"only_on": {a_host: act_only_a, b_host: act_only_b},
                        "differing": act_diff,
                        "counts": {a_host: len(a_act), b_host: len(b_act)}},
            "services": {"only_on": {a_host: svc_only_a,
                                     b_host: svc_only_b},
                         "differing_desired_state": svc_diff,
                         "counts": {a_host: len(a_svc), b_host: len(b_svc)}},
            "summary": {"apps_only_on_one_host": len(apps_only_a)
                        + len(apps_only_b),
                        "actions_only_on_one_host": len(act_only_a)
                        + len(act_only_b),
                        "actions_differing": len(act_diff),
                        "services_only_on_one_host": len(svc_only_a)
                        + len(svc_only_b),
                        "services_differing_desired_state": len(svc_diff)}}


# --- screen sharing switch (v1.6.0) ---------------------------------------
# Durable on/off switch for Apple Screen Sharing on this host. The MCP
# runs as the host user (unprivileged); status is probed without privilege
# (screensharingd process + TCP connect to 127.0.0.1:5900 + the access
# group's membership read via dscl). State changes go through the
# ARDAgent kickstart binary under sudo -n, authorized by a narrowly
# scoped sudoers drop-in (/etc/sudoers.d/factotum-screensharing:
# NOPASSWD for that one SIP-protected binary only).

KICKSTART = ("/System/Library/CoreServices/RemoteManagement/"
             "ARDAgent.app/Contents/Resources/kickstart")
DSEDITGROUP = "/usr/sbin/dseditgroup"
SS_ACCESS_GROUP = "com.apple.access_screensharing"
# Public export: the one user allowed to connect is the host's
# own user (FACTOTUM_SCREEN_SHARING_USER overrides).
SCREEN_SHARING_USER = (ENV.get("FACTOTUM_SCREEN_SHARING_USER")
                       or os.environ.get("USER", ""))


def _ss_run(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as e:
        return -1, str(e)


def _ss_listening():
    try:
        with socket.create_connection(("127.0.0.1", 5900), timeout=2):
            return True
    except Exception:
        return False


def _ss_group_name(guid):
    """Resolve a nested group's GeneratedUID to its record name."""
    rc, out = _ss_run(["dscl", ".", "-search", "/Groups",
                       "GeneratedUID", guid])
    if rc == 0 and out:
        return out.split()[0]
    return guid


def _screensharing_status():
    rc, _out = _ss_run(["pgrep", "-x", "screensharingd"])
    daemon = rc == 0
    listening = _ss_listening()
    users = []
    nested = []
    rc, out = _ss_run(["dscl", ".", "-read", "/Groups/" + SS_ACCESS_GROUP])
    if rc == 0:
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("GroupMembership:"):
                users += line.split(":", 1)[1].split()
            elif line.startswith("NestedGroups:"):
                nested += [_ss_group_name(g)
                           for g in line.split(":", 1)[1].split()]
    return {"enabled": bool(daemon or listening),
            "daemon_running": daemon,
            "listening_5900": listening,
            "allowed_users": users,
            "allowed_nested_groups": nested}


def _ss_normalize_access():
    """Converge the access group to exactly the configured user: a GUI
    enable can leave the local admin group nested (any admin may
    connect); the policy is that one user only. Idempotent; failures are ignored because
    the desired state may already hold (dseditgroup errors on a no-op
    delete). Returns nothing; callers re-read status."""
    _ss_run(["sudo", "-n", DSEDITGROUP, "-o", "edit", "-n", "/Local/Default",
             "-d", "admin", "-t", "group", SS_ACCESS_GROUP])
    _ss_run(["sudo", "-n", DSEDITGROUP, "-o", "edit", "-n", "/Local/Default",
             "-a", SCREEN_SHARING_USER, "-t", "user", SS_ACCESS_GROUP])


def tool_screensharing_status(args):
    return _screensharing_status()


def tool_screensharing_set(args):
    enabled = bool(args.get("enabled"))
    before = _screensharing_status()
    if before["enabled"] == enabled and before["listening_5900"] == enabled:
        out = {"changed": False, "note": "already in the requested state"}
        out.update(before)
        return out
    if enabled:
        cmd = ["sudo", "-n", KICKSTART, "-activate", "-configure",
               "-access", "-on", "-users", SCREEN_SHARING_USER, "-privs", "-all",
               "-restart", "-agent"]
    else:
        cmd = ["sudo", "-n", KICKSTART, "-deactivate", "-configure",
               "-access", "-off"]
    rc, output = _ss_run(cmd, timeout=90)
    if enabled and rc == 0:
        _ss_normalize_access()
    time.sleep(3)
    after = _screensharing_status()
    out = {"changed": (after["enabled"] != before["enabled"]
                       or after["listening_5900"]
                       != before["listening_5900"]),
           "kickstart_rc": rc,
           "kickstart_output_tail": output[-400:]}
    out.update(after)
    if rc != 0:
        out["error"] = ("kickstart failed (rc=%s); screen sharing changes "
                        "need the sudoers drop-in "
                        "/etc/sudoers.d/factotum-screensharing" % rc)
    return out


# ---------------------------------------------------------------------
# Photo-index live queries (1.13.0, internal ticket)
#
# Point queries against this host's Photos ML stores, answered by the
# server itself (the screensharing-probe posture), not by the app:
#   leo.sqlite  (per Photos library)  items -> lexeme_ids (little-endian
#               int32 array) -> lexicon (lexeme_id, category, content)
#   VUIndex.sqlite (main library)     ZVUINDEXOBSERVATION (per-asset
#               128-dim float32 embeddings) + ZVUINDEXMAPPING
#               (label / partition / partner / similarity clusters)
# Every store opens with SQLite mode=ro: a write through these tools
# is impossible at the SQLite layer. Embeddings are compared on-host
# and never returned. All result sets are bounded by explicit limits.

LEO_LIBRARY_PATHS = {
    "main": "~/Pictures/Photos Library.photoslibrary/database/search/leo.sqlite",
    "syndication": "~/Library/Photos/Libraries/Syndication.photoslibrary/database/search/leo.sqlite",
}
VU_INDEX_PATH = ("~/Pictures/Photos Library.photoslibrary/private/"
                 "com.apple.mediaanalysisd/caches/vision/VUIndex.sqlite")
# The mirror's asset grain (leo-mirror-ingest scan_leo): items of
# type 1 and 6 are assets; other item types are collections/moments.
LEO_ASSET_ITEM_TYPES = (1, 6)
VU_EMBEDDING_BYTES = 512  # 128 x float32


def _bounded_limit(args, default, maximum):
    try:
        n = int(args.get("limit", default))
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, maximum))


def _store_freshness(path):
    apath = os.path.expanduser(path)
    try:
        st = os.stat(apath)
    except OSError:
        return {"path": apath, "exists": False}
    return {"path": apath, "exists": True, "size_bytes": st.st_size,
            "mtime": datetime.fromtimestamp(
                st.st_mtime, timezone.utc).isoformat()}


def _photo_store_open(path):
    """(connection, error): a Photos ML store opened read-only.

    mode=ro is the read-only guarantee (any write raises at the
    SQLite layer); PRAGMA query_only restates the intent in-band.
    """
    apath = os.path.expanduser(path)
    if not os.path.exists(apath):
        return None, {"error": "store not present on this host: %s" % apath}
    try:
        conn = sqlite3.connect("file:" + quote(apath) + "?mode=ro",
                               uri=True, timeout=30)
    except sqlite3.Error as e:
        return None, {"error": "cannot open store read-only: %s (%s)"
                        % (apath, e)}
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn, None


def _leo_open(library):
    path = LEO_LIBRARY_PATHS.get(library)
    if path is None:
        return None, None, {"error": "unknown leo library %r (expected: %s)"
                            % (library,
                               ", ".join(sorted(LEO_LIBRARY_PATHS)))}
    conn, err = _photo_store_open(path)
    return conn, path, err


def _unpack_lexeme_ids(blob):
    if not blob or len(blob) % 4:
        return None
    return struct.unpack("<%di" % (len(blob) // 4), blob)


def _like_escape(text):
    return (text.replace("\\", "\\\\").replace("%", "\\%")
            .replace("_", "\\_"))


def tool_leo_asset_terms(args):
    asset = str(args.get("asset") or "").strip()
    if not asset:
        return {"error": "asset is required (a Photos asset UUID)"}
    library = str(args.get("library") or "main")
    limit = _bounded_limit(args, 200, 1000)
    conn, path, err = _leo_open(library)
    if err:
        return err
    try:
        row = conn.execute(
            "SELECT identifier, type, lexeme_ids FROM items "
            "WHERE identifier = ?", (asset,)).fetchone()
        if row is None and asset.upper() != asset:
            row = conn.execute(
                "SELECT identifier, type, lexeme_ids FROM items "
                "WHERE identifier = ?", (asset.upper(),)).fetchone()
        out = {"host": HOST_LABEL, "library": library,
               "store": _store_freshness(path), "asset": asset}
        if row is None:
            out["found"] = False
            return out
        ids = _unpack_lexeme_ids(row["lexeme_ids"]) or []
        ordered, seen = [], set()
        for lid in ids:
            if lid not in seen:
                seen.add(lid)
                ordered.append(lid)
        terms = []
        for i in range(0, len(ordered), 500):
            chunk = ordered[i:i + 500]
            marks = ",".join("?" * len(chunk))
            # A lexeme_id names a concept; the lexicon holds one row
            # per surface form (the type-1 row is the canonical form,
            # type-2 rows are synonyms/variants). All forms are
            # terms; only empty-content lexemes are structural
            # markers (they still count in lexeme_count below).
            got = {}
            for r in conn.execute(
                    "SELECT lexeme_id, type, category, content "
                    "FROM lexicon WHERE lexeme_id IN (%s) "
                    "ORDER BY type, pk" % marks, chunk):
                got.setdefault(r["lexeme_id"], []).append(r)
            for lid in chunk:
                for r in got.get(lid, []):
                    if r["content"]:
                        terms.append({"lexeme_id": r["lexeme_id"],
                                      "category": r["category"],
                                      "content": r["content"],
                                      "lexicon_type": r["type"],
                                      "canonical": r["type"] == 1})
        out.update({"found": True, "item_type": row["type"],
                    "lexeme_count": len(ordered),
                    "term_count": len(terms), "terms": terms[:limit],
                    "truncated": len(terms) > limit})
        return out
    finally:
        conn.close()


def _leo_holders(conn, lexeme_id):
    """(asset holders, other-item holder count) for one lexeme.

    instr() finds candidate blobs in C; the unpack + membership
    check makes the result exact (a 4-byte pattern can straddle a
    lexeme boundary inside the blob).
    """
    pat = struct.pack("<i", lexeme_id)
    assets, others, seen = [], 0, set()
    for r in conn.execute(
            "SELECT identifier, type, lexeme_ids FROM items "
            "WHERE instr(lexeme_ids, ?) > 0", (pat,)):
        ids = _unpack_lexeme_ids(r["lexeme_ids"])
        if not ids or lexeme_id not in ids:
            continue
        if r["type"] in LEO_ASSET_ITEM_TYPES:
            if r["identifier"] not in seen:
                seen.add(r["identifier"])
                assets.append({"asset": r["identifier"],
                               "item_type": r["type"]})
        else:
            others += 1
    return assets, others


def tool_leo_term_assets(args):
    term = str(args.get("term") or "").strip()
    if not term:
        return {"error": "term is required"}
    match = str(args.get("match") or "exact")
    if match not in ("exact", "prefix", "contains"):
        return {"error": "match must be exact, prefix or contains"}
    library = str(args.get("library") or "main")
    limit = _bounded_limit(args, 100, 500)
    category = args.get("category")
    if category is not None:
        try:
            category = int(category)
        except (TypeError, ValueError):
            return {"error": "category must be an integer"}
    conn, path, err = _leo_open(library)
    if err:
        return err
    try:
        if match == "exact":
            where, params = "lower(content) = lower(?)", [term]
        elif match == "prefix":
            where = "lower(content) LIKE lower(?) ESCAPE '\\'"
            params = [_like_escape(term) + "%"]
        else:
            where = "lower(content) LIKE lower(?) ESCAPE '\\'"
            params = ["%" + _like_escape(term) + "%"]
        if category is not None:
            where += " AND category = ?"
            params.append(category)
        rows = conn.execute(
            "SELECT lexeme_id, type, category, content FROM lexicon "
            "WHERE content IS NOT NULL AND content <> '' AND " + where +
            " ORDER BY category, type, content LIMIT 201",
            params).fetchall()
        # A term can match several surface forms of one lexeme;
        # holders are a property of the lexeme, so group by
        # lexeme_id and present the canonical (type-1) form first.
        grouped = {}
        for r in rows:
            grouped.setdefault(r["lexeme_id"], []).append(r)
        lexemes = list(grouped.values())
        out = {"host": HOST_LABEL, "library": library,
               "store": _store_freshness(path), "term": term,
               "match": match,
               "lexemes_truncated": len(lexemes) > 50}
        lexeme_views, by_asset, other_items = [], {}, 0
        for forms in lexemes[:50]:
            lex = forms[0]
            holders, others = _leo_holders(conn, lex["lexeme_id"])
            other_items += others
            lexeme_views.append({
                "lexeme_id": lex["lexeme_id"], "category": lex["category"],
                "content": lex["content"], "lexicon_type": lex["type"],
                "matched_forms": [f["content"] for f in forms],
                "asset_count": len(holders)})
            for h in holders:
                slot = by_asset.setdefault(
                    h["asset"],
                    {"asset": h["asset"], "item_type": h["item_type"],
                     "lexeme_ids": []})
                slot["lexeme_ids"].append(lex["lexeme_id"])
        assets = list(by_asset.values())
        out.update({"lexemes": lexeme_views,
                    "total_assets": len(assets),
                    "assets": assets[:limit],
                    "other_items": other_items,
                    "truncated": len(assets) > limit})
        return out
    finally:
        conn.close()


def tool_leo_stats(args):
    library = str(args.get("library") or "all")
    names = (list(LEO_LIBRARY_PATHS) if library == "all" else [library])
    blocks = []
    for name in names:
        conn, path, err = _leo_open(name)
        if err:
            blocks.append({"library": name, "error": err["error"],
                           "store": _store_freshness(
                               LEO_LIBRARY_PATHS.get(name, ""))})
            continue
        try:
            one = lambda sql: conn.execute(sql).fetchone()[0]
            blocks.append({
                "library": name, "store": _store_freshness(path),
                "lexicon_rows": one("SELECT COUNT(*) FROM lexicon"),
                "content_lexemes": one(
                    "SELECT COUNT(*) FROM lexicon "
                    "WHERE content IS NOT NULL AND content <> ''"),
                "items": one("SELECT COUNT(*) FROM items"),
                "asset_items": one(
                    "SELECT COUNT(*) FROM items WHERE type IN (1, 6)"),
                "categories": one(
                    "SELECT COUNT(DISTINCT category) FROM lexicon"),
            })
        finally:
            conn.close()
    return {"host": HOST_LABEL, "libraries": blocks}


def _vu_open():
    return _photo_store_open(VU_INDEX_PATH)


def _asset_bytes(asset):
    try:
        return uuidlib.UUID(str(asset).strip()).bytes
    except (ValueError, AttributeError, TypeError):
        return None


def _uuid_text(blob):
    if blob is not None and len(blob) == 16:
        # The stores' canonical text form is uppercase (as in leo
        # item identifiers and Photos UUIDs); Python renders lowercase.
        return str(uuidlib.UUID(bytes=bytes(blob))).upper()
    return None


def tool_vu_asset_observations(args):
    raw = str(args.get("asset") or "").strip()
    blob = _asset_bytes(raw)
    if blob is None:
        return {"error": "asset must be a UUID, got %r" % raw}
    limit = _bounded_limit(args, 50, 200)
    conn, err = _vu_open()
    if err:
        return err
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM ZVUINDEXOBSERVATION WHERE ZASSET = ?",
            (blob,)).fetchone()[0]
        out = {"host": HOST_LABEL, "store": _store_freshness(VU_INDEX_PATH),
               "asset": _uuid_text(blob),
               "observation_count": total}
        if not total:
            out["found"] = False
            out["observations"] = []
            return out
        rows = conn.execute(
            "SELECT o.Z_PK pk, o.ZIDENTIFIER identifier, o.ZTYPE type, "
            "o.ZSOURCE source, o.ZCLIENT client, "
            "o.ZISPRIMARY is_primary, o.ZCONFIDENCE confidence, "
            "o.ZQUALITY quality, o.ZMOMENT moment, "
            "(o.ZEMBEDDING IS NOT NULL) has_embedding, "
            "(o.ZCONTEXTUALEMBEDDING IS NOT NULL) has_contextual_embedding, "
            "m.ZLABEL label, m.ZPARTITION partition, "
            "m.ZPARTNER partner, m.ZSIMILARITY similarity, "
            "m.ZDENSITY density, m.ZDIVERSITY diversity, "
            "m.ZCLUSTERINGSTATUS clustering_status "
            "FROM ZVUINDEXOBSERVATION o "
            "LEFT JOIN ZVUINDEXMAPPING m ON m.Z_PK = o.ZMAPPING "
            "WHERE o.ZASSET = ? "
            "ORDER BY o.ZISPRIMARY DESC, o.ZCONFIDENCE DESC, o.Z_PK "
            "LIMIT ?", (blob, limit)).fetchall()
        partners = {}
        for r in rows:
            if r["partner"] not in (None, -1) and r["partner"] not in partners:
                prow = conn.execute(
                    "SELECT ZASSET FROM ZVUINDEXOBSERVATION "
                    "WHERE ZIDENTIFIER = ? LIMIT 1",
                    (r["partner"],)).fetchone()
                partners[r["partner"]] = (
                    _uuid_text(prow[0]) if prow else None)
        observations = []
        for r in rows:
            mapping = None
            if r["label"] is not None:
                mapping = {
                    "label": r["label"], "partition": r["partition"],
                    "partner": r["partner"],
                    "partner_asset": partners.get(r["partner"]),
                    "similarity": r["similarity"],
                    "density": r["density"], "diversity": r["diversity"],
                    "clustering_status": r["clustering_status"]}
            observations.append({
                "pk": r["pk"], "identifier": r["identifier"],
                "type": r["type"], "source": r["source"],
                "client": r["client"],
                "is_primary": bool(r["is_primary"]),
                "confidence": r["confidence"], "quality": r["quality"],
                "moment": _uuid_text(r["moment"]),
                "has_embedding": bool(r["has_embedding"]),
                "has_contextual_embedding":
                    bool(r["has_contextual_embedding"]),
                "mapping": mapping})
        out.update({"found": True, "observations": observations,
                    "truncated": total > limit})
        return out
    finally:
        conn.close()


def tool_vu_label_assets(args):
    try:
        label = int(args.get("label"))
    except (TypeError, ValueError):
        return {"error": "label is required (an integer label id)"}
    limit = _bounded_limit(args, 100, 500)
    conn, err = _vu_open()
    if err:
        return err
    try:
        total = conn.execute(
            "SELECT COUNT(DISTINCT o.ZASSET) FROM ZVUINDEXMAPPING m "
            "JOIN ZVUINDEXOBSERVATION o ON o.Z_PK = m.ZOBSERVATION "
            "WHERE m.ZLABEL = ? AND o.ZASSET IS NOT NULL",
            (label,)).fetchone()[0]
        rows = conn.execute(
            "SELECT o.ZASSET asset, COUNT(*) observations, "
            "MAX(m.ZSIMILARITY) best_similarity FROM ZVUINDEXMAPPING m "
            "JOIN ZVUINDEXOBSERVATION o ON o.Z_PK = m.ZOBSERVATION "
            "WHERE m.ZLABEL = ? AND o.ZASSET IS NOT NULL "
            "GROUP BY o.ZASSET ORDER BY best_similarity DESC LIMIT ?",
            (label, limit)).fetchall()
        return {"host": HOST_LABEL,
                "store": _store_freshness(VU_INDEX_PATH),
                "label": label, "total_assets": total,
                "assets": [{"asset": _uuid_text(r["asset"]),
                            "observations": r["observations"],
                            "best_similarity": r["best_similarity"]}
                           for r in rows],
                "truncated": total > limit}
    finally:
        conn.close()


def _vu_representative(conn, asset_blob):
    return conn.execute(
        "SELECT Z_PK, ZEMBEDDING FROM ZVUINDEXOBSERVATION "
        "WHERE ZASSET = ? AND ZEMBEDDING IS NOT NULL "
        "ORDER BY ZISPRIMARY DESC, ZCONFIDENCE DESC, Z_PK LIMIT 1",
        (asset_blob,)).fetchone()


# The similarity pool (one representative embedding + its norm per
# asset) costs a full scan of the embedding rows to build, so it is
# cached in-process, keyed on the store's (size, mtime) fingerprint
# including the -wal sidecar: mediaanalysisd writes in bursts, and
# any write moves the fingerprint and forces a rebuild.
_VU_POOL_CACHE = {"key": None, "pool": None, "skipped": 0}
_VU_POOL_LOCK = threading.Lock()


def _vu_pool_key():
    apath = os.path.expanduser(VU_INDEX_PATH)
    parts = []
    for suffix in ("", "-wal"):
        try:
            st = os.stat(apath + suffix)
            parts.append((st.st_size, st.st_mtime_ns))
        except OSError:
            parts.append(None)
    return tuple(parts)


def _vu_pool(conn):
    key = _vu_pool_key()
    with _VU_POOL_LOCK:
        if _VU_POOL_CACHE["key"] == key and _VU_POOL_CACHE["pool"] is not None:
            return _VU_POOL_CACHE["pool"], _VU_POOL_CACHE["skipped"]
        pool, skipped = {}, 0
        for r in conn.execute(
                "SELECT ZASSET, ZEMBEDDING FROM ZVUINDEXOBSERVATION "
                "WHERE ZEMBEDDING IS NOT NULL AND ZASSET IS NOT NULL "
                "ORDER BY ZASSET, ZISPRIMARY DESC, ZCONFIDENCE DESC"):
            if r["ZASSET"] in pool:
                continue
            emb = r["ZEMBEDDING"]
            if len(emb) != VU_EMBEDDING_BYTES:
                skipped += 1
                continue
            vec = array("f", emb)
            pool[r["ZASSET"]] = (vec, sum(x * x for x in vec) ** 0.5)
        _VU_POOL_CACHE.update({"key": key, "pool": pool,
                               "skipped": skipped})
        return pool, skipped


def tool_vu_similar_assets(args):
    raw = str(args.get("asset") or "").strip()
    blob = _asset_bytes(raw)
    if blob is None:
        return {"error": "asset must be a UUID, got %r" % raw}
    limit = _bounded_limit(args, 10, 50)
    try:
        min_similarity = float(args.get("min_similarity", 0.0))
    except (TypeError, ValueError):
        return {"error": "min_similarity must be a number"}
    conn, err = _vu_open()
    if err:
        return err
    try:
        out = {"host": HOST_LABEL, "store": _store_freshness(VU_INDEX_PATH),
               "asset": _uuid_text(blob)}
        target = _vu_representative(conn, blob)
        if target is None:
            out.update({"found": False,
                        "note": "asset has no embedding-bearing "
                                "observation in this host's VUIndex"})
            return out
        if len(target["ZEMBEDDING"]) != VU_EMBEDDING_BYTES:
            out.update({"found": False,
                        "note": "asset embedding has unexpected size %d"
                                % len(target["ZEMBEDDING"])})
            return out
        tvec = array("f", target["ZEMBEDDING"])
        tnorm = sum(x * x for x in tvec) ** 0.5
        pool, skipped = _vu_pool(conn)
        scored = []
        for other, (vec, cnorm) in pool.items():
            if other == blob:
                continue
            if not tnorm or not cnorm:
                continue
            sim = sum(map(mul, tvec, vec)) / (tnorm * cnorm)
            if sim >= min_similarity:
                scored.append((sim, other))
        scored.sort(key=lambda p: p[0], reverse=True)
        out.update({
            "found": True,
            "target_observation": target["Z_PK"],
            "pool_assets": len(pool), "pool_skipped": skipped,
            "similar": [{"asset": _uuid_text(b),
                         "similarity": round(s, 6)}
                        for s, b in scored[:limit]]})
        return out
    finally:
        conn.close()


DISPATCH = {
    "screensharing_status": tool_screensharing_status,
    "screensharing_set": tool_screensharing_set,
    "factotum_apps": tool_apps,
    "factotum_actions": tool_actions,
    "factotum_invoke": tool_invoke,
    "factotum_registry": tool_registry,
    "factotum_discovery_run": tool_discovery_run,
    "factotum_services": tool_services,
    "factotum_service_invoke": tool_service_invoke,
    "factotum_health": tool_health,
    "factotum_registry_sync": tool_registry_sync,
    "factotum_peer_registry": tool_peer_registry,
    "factotum_parity": tool_parity,
    "factotum_candidates": tool_candidates,
    "factotum_capability_coverage": tool_capability_coverage,
    "factotum_leo_asset_terms": tool_leo_asset_terms,
    "factotum_leo_term_assets": tool_leo_term_assets,
    "factotum_leo_stats": tool_leo_stats,
    "factotum_vu_asset_observations": tool_vu_asset_observations,
    "factotum_vu_label_assets": tool_vu_label_assets,
    "factotum_vu_similar_assets": tool_vu_similar_assets,
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        if SECRET and self.headers.get("X-Origin-Secret") != SECRET:
            self._send(403, {"error": "forbidden"})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except Exception:
            self._send(400, {"error": "bad json"})
            return
        method = body.get("method", "")
        rid = body.get("id")
        if method == "initialize":
            self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "factotum-mcp", "version": VERSION}}},
                session="factotum-session")
            return
        if method.startswith("notifications/"):
            self._send(202, None)
            return
        if method == "tools/list":
            self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
            return
        if method == "tools/call":
            params = body.get("params", {})
            name = params.get("name", "")
            fn = DISPATCH.get(name)
            if not fn:
                self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": "unknown tool " + name}],
                    "isError": True}})
                return
            try:
                result = fn(params.get("arguments", {}))
            except Exception as e:
                result = {"error": "tool failed: %s" % e}
            self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps(result, indent=1)}]}})
            return
        self._send(200, {"jsonrpc": "2.0", "id": rid,
                         "error": {"code": -32601, "message": "unknown method"}})

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "service": "factotum-mcp",
                             "version": VERSION, "host": HOST_LABEL})
            return
        self._send(404, {"error": "not found"})

    def _send(self, code, obj, session=None):
        data = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if session:
            self.send_header("Mcp-Session-Id", session)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)


if __name__ == "__main__":
    # Capability companion: kick a scan if its state is stale/missing.
    maybe_kick_capability_scan()
    # Registry sync at startup: background thread, never blocks serving.
    trigger_sync_async("startup")
    # App sync-request watcher (menu/window "Sync Now" lands here).
    start_sync_request_watcher()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
