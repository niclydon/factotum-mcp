# Factotum-MCP

> Public export of factotum-mcp, generated from a private working
> repository. The code, docs, and tests here are complete and usable
> as published; deployment-specific details (real hostnames,
> addresses, and account names) are replaced with worked examples:
> the two example hosts are `host1` and `host2`, the operator's
> account is `example`, and the sync backend is a Postgres database.
> The private per-host registry seed is replaced by a synthetic
> example (`server/example-seed-registry.py`), and gateway callers
> take their token from the `FACTOTUM_GATEWAY_TOKEN` environment
> variable.

The agent-facing half of Factotum: a small MCP server deployed per Mac host
(Host1 and Host2) that exposes that host's Factotum registry as tools, plus
the callers agents use from the VM.

The app itself (signed bundle, TCC grants, native action handlers) lives in
the sibling `factotum` repo. This repo is shared code: the same server runs
on both hosts; only the host's registry and secrets differ.

factotum-mcp is the sync/registry plane (registry custody, Postgres sync,
health rendering); it is NOT the gateway. The per-host MCP front door is
factotum-gateway (`gateway/` in this repo, tailnet 7902, its own
LaunchAgent), which never holds the Postgres DSN.

## Server

- Python standard library only, modeled on photopull-mcp.
- Loopback only, port 7901 on each host.
- LaunchAgent `io.example.factotum-mcp` (RunAtLoad + KeepAlive).
- Origin secret generated per host into the server's `.env` (0600 on the
  host). It never leaves the host: callers read it transiently over SSH,
  the same pattern as photopull-mcp-call.
- The server reads the host's `registry.json` live, so discovery results
  show up as tools without a server change.

## Tools (registry-driven)

- `factotum_apps` — installed/running apps from the registry.
- `factotum_actions` — actions, optionally filtered by app.
- `factotum_invoke` — invoke a **native** action by id with params.
  Catalog actions return their owning tool's pointer; they are not invoked
  through here in v1.
- `factotum_registry` — the raw registry (the map itself is a deliverable).
- `factotum_discovery_run` — trigger a fresh discovery pass and registry
  rewrite.
- `factotum_candidates` / `factotum_capability_coverage` — capability
  research (1.7.0): Tier 2 candidate action records (status=candidate,
  never invocable) and the per-host coverage + Postgres parity summary,
  served from the factotum-capability companion's state.
- `factotum_leo_asset_terms` / `factotum_leo_term_assets` /
  `factotum_leo_stats` — live leo queries (1.13.0): per-host point
  queries against leo.sqlite (main + Syndication libraries) —
  an asset's terms, the assets carrying a term, store counts.
- `factotum_vu_asset_observations` / `factotum_vu_label_assets` /
  `factotum_vu_similar_assets` — live VUIndex queries (1.13.0):
  a Photos asset's Visual Understanding observations, the assets
  under a label id, and nearest assets by embedding cosine
  similarity computed on-host (embeddings never leave the Mac).
  All photo-index tools are read-only: the stores open with
  SQLite `mode=ro`, and every result set is limit-bounded.

Refusal rule: `factotum_invoke` refuses any action whose `op` is `delete`
or whose `risk` is `outward`, with a "requires the owner approval" error. There is
no override flag; widening happens in chat, with the owner, or not at all.

## Supervision and health

The registry's `services` section covers the Mac utilities Factotum manages
(PhotoPull, the Logbook daemon, and peers). Their supervision actions
(status, start, stop, restart, logs) are ordinary registry actions invoked
through `factotum_invoke`, and `factotum_registry` / `factotum_actions`
surface the per-host health roll-up: one call answers whether everything
Factotum manages on that host is in its desired state. During transition
the sibling MCPs remain directly reachable; the end state is that agents
reach Mac capabilities through this server only.

## Callers (from the agent VM)

- `factotum-mcp-call` — Host1 instance.
- `factotum-mcp-call-host2` — Host2 instance.

Both follow the photopull-mcp-call pattern: the client runs on the host over
SSH stdin; the secret is read on the host and never lands on the VM.

## This repo

- `server/` — the MCP server source.
- `callers/` — VM-side caller scripts.
- `docs/` — tool contract and deployment notes.

This export's public history starts at CHANGES.md in this repo.

## License

Apache License 2.0; see `LICENSE`.
