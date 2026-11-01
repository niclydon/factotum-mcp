# factotum-gateway

The per-host MCP front door for Factotum (Factotum 2.0, workstream W1).
One gateway process per Mac, deployed to `~/services/factotum-gateway/`
on each host under its own LaunchAgent `io.example.factotum-gateway`,
serving MCP streamable HTTP on port 7902. Version 1.0.0, versioned
independently of factotum-mcp in this same repo. Python standard
library only; no codesign is involved (it is a pure Python service and
holds no TCC grant of its own — it acts through Factotum.app's file
contract, under that host's identity).

## Posture (the binding decisions)

- **D1 — per-host.** One gateway per Mac. Each gateway dispatches only
  through its own host's Factotum.app inbox/outbox and reads only its
  own host's registry.
- **D2 — one token per host.** A single shared bearer token per host
  serves all consumers (this overrides the design draft's per-consumer
  tokens). Accepted tradeoff, recorded in the design: a leak re-keys
  every consumer of that host at once, and the audit's consumer field
  is a caller claim (the `X-Consumer` header or a `consumer` argument,
  default `unknown`; the VM callers claim `caller` unless
  `FACTOTUM_GATEWAY_CONSUMER` says otherwise), not a cryptographic
  identity. The token is generated on the host
  (`openssl rand -hex 32`) into `~/services/factotum-gateway/.env`
  (0600, gateway vars only: token, bind, port, host label). On the
  caller side, the public wrappers take it from the
  `FACTOTUM_GATEWAY_TOKEN` environment variable (with
  `FACTOTUM_GATEWAY_BIND` / `FACTOTUM_GATEWAY_PORT` naming the
  target); they read no token file. It never appears in commits,
  logs, or reports.
- **D3 — factotum-mcp is not this.** factotum-mcp stays the separate
  sync/registry plane on loopback 7901. The gateway never holds or
  reads the Postgres DSN and never reads factotum-mcp's `.env`. A
  compromised door yields no database credentials.
- **D10 — preview for delete/outward.** See below.
- **D12 — tailnet only.** The gateway binds the host's tailnet IP from
  its config (`GATEWAY_BIND`) and nothing else: no loopback listener,
  no LAN listener. It refuses to start without an explicit bind and
  token.

## The generated surface

There is no hand-maintained tool list. The surface is generated from
the live registry file (`~/Library/Application
Support/Factotum/registry.json`, plus the capability companion's
candidates, mirroring factotum-mcp) and regenerated whenever the
registry file's mtime changes — checked on every `tools/list` and
`tools/call`, no restart needed.

- One tool per registry action. The tool name is the action id with
  dots/dashes (any non-word character) sanitized to underscores, e.g.
  `eventkit.reminder_create` becomes `eventkit_reminder_create`. The
  description comes from the registry record. The input schema is
  `{params: object, preview_token: string (optional), consumer:
  string (optional)}`; `params` is a free-form passthrough with the
  same shape as factotum-mcp's `factotum_invoke` params.
- One tool per registered service, named `service.<id>` with the id
  sanitized the same way (e.g. `service.factotum_mcp`). Its schema has
  an `action` enum taken from the service's own `actions[]`, plus the
  `update` arguments (`expected_sha256`, `expected_commit`).
- `gateway.health` — the gateway's own health: version, host, bind,
  registry freshness, surface counts.
- `gateway.catalog` — a read tool rendering the whole registry as
  data: every action with its disposition and pointer, every service.

Dispatch and refusal semantics mirror factotum-mcp's `factotum_invoke`
exactly, because the gateway is a door, not a second policy brain:

- `status=candidate` refuses always, before any other gate, and no
  preview is issued for a candidate.
- `status=catalog` is never dispatched; the owning tool's pointer is
  returned. Catalog actions still get their own generated tools (this
  is the rendering chosen for non-native actions), and those tools
  return the pointer/refusal without dispatching.
- `status=unavailable` errors.
- `status=native` dispatches to Factotum.app via the inbox/outbox
  file contract: `inbox/<id>.json` `{"id", "op", "params"}`, result in
  `outbox/<id>.json`. Op names derive from action ids exactly as
  factotum-mcp's `op_for_action`/`KNOWN_OPS` do. Service operations
  dispatch as `service_status`/`service_start`/`service_stop`/
  `service_restart`/`service_logs_recent`/`service_update` with
  params `{service_id, ...}`. The three self actions
  (`factotum_health`, `factotum_registry_read`, `factotum_logs_recent`)
  keep factotum-mcp's local fallback when the app does not answer.

Timeouts mirror factotum-mcp: 90s default, 180s for `discovery_run`,
210s for `service_update` (60s for other service operations, as in
`factotum_service_invoke`). Any single tool result's text is capped at
32768 chars; a truncated result carries a visible marker naming the
full char/byte count, and the audit records the full byte count.

## The D10 preview flow

An action whose registry record has `op == "delete"` or `risk` in
(`delete`, `outward`) refuses a plain call with:

```json
{"refused": true, "preview_required": true,
 "preview": {"action_id": "...", "description": "<concrete
   what-will-happen text built from the registry record: name,
   mechanism, target service/tool, the params echoed>",
   "op": "...", "risk": "...", "status": "...", "params": {}},
 "preview_token": "<random, single-use>"}
```

Preview tokens are random, held host-side in gateway memory only,
bound to (action_id + canonical params hash), single-use, and expire
after 300s. A call carrying a valid token proceeds:

- native action: it dispatches, once;
- catalog action: it returns the catalog pointer disposition,
  explicitly noting the owning tool's own gates still apply (Disarm
  Blink still needs its own per-run approval at the Shortcuts bridge;
  the gateway's preview is not a substitute).

An invalid, expired, reused, or params-mismatched token is refused
with a fresh preview issued.

## Audit

One JSON line per tool call, appended host-side to
`~/Library/Application Support/FactotumGateway/audit.jsonl`:

```json
{"ts", "host", "consumer", "tool", "action_id" | "service_id",
 "outcome": "ok|refused|error|timeout", "duration_ms",
 "result_bytes", "preview": bool}
```

`preview` is true when the call involved the preview flow (a token
was presented or a preview was issued). The audit is host-side only;
nothing in it leaves the host through the gateway.

## Callers

`callers/factotum_gateway_client.py` plus the wrappers
`factotum-gateway-call` (host1) and `factotum-gateway-call-host2`,
following the caller pattern: the client runs on the host over
SSH stdin; the token is supplied to the wrapper through the
`FACTOTUM_GATEWAY_TOKEN` environment variable and passed to the
client via a stdin payload, never argv.

```
factotum-gateway-call __list
factotum-gateway-call factotum_health '{}'
factotum-gateway-call-host2 shortcuts_disarm_blink '{"params": {}}'
```

## Tests

`gateway/tests/test_gateway.py` (stdlib unittest) spins the gateway
against a fixture registry and a fake Factotum.app answering the file
contract. It covers auth, exact surface generation, dispositions, the
full D10 flow (including native dispatch with a token against a
harmless fixture op, param-hash binding, single-use, expiry), service
dispatch, timeout, truncation, audit shape, and mtime regeneration.

```
python3 -m unittest discover -s gateway/tests -v
```

## What the gateway is not

Not the sync plane (that is factotum-mcp, D3). Not a policy brain:
every refusal and gate comes from the registry record, rendered
faithfully. Not internet- or LAN-facing (D12). Not a session server:
requests are stateless, like factotum-mcp.
