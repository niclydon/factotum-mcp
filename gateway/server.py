#!/usr/bin/env python3
"""factotum-gateway: the per-host MCP front door for Factotum (Factotum 2.0
W1, decisions D1/D2/D3/D10/D12).

One gateway per Mac, acting under that host's identity (D1). It exposes the
host's Factotum registry as a generated MCP tool surface over streamable
HTTP on the host's TAILNET interface only (D12), authenticated by ONE
shared bearer token per host for all consumers (D2). The token lives only
in this service's own .env (0600) beside this file.

Privilege separation (D3): factotum-mcp remains the separate sync/registry
plane. This gateway NEVER holds or reads the Postgres DSN and NEVER reads
factotum-mcp's .env. It reads exactly two things on the host: its own
.env (token/bind/port) and the Factotum registry file (plus the
capability companion's candidates file, read-only, mirroring
factotum-mcp's candidate handling).

Dispatch contract: identical to factotum-mcp's. Native actions dispatch
through the Factotum.app inbox/outbox file contract
(~/Library/Application Support/Factotum/{inbox,outbox}); op names derive
from action ids exactly as factotum-mcp's op_for_action/KNOWN_OPS do;
service operations dispatch as service_status/service_start/... with
params {service_id, ...}. Refusal semantics mirror factotum_invoke:
status=candidate refuses always; status=catalog returns the pointer and
is never dispatched; status=unavailable errors.

D10 preview rule: an action whose registry record has op == "delete" or
risk in ("delete", "outward") — or security_class == "credential"
(break-glass enable/rotate, gateway 1.0.1) — refuses a plain call and
returns a preview
(what would happen, built from the registry record) plus a single-use
preview token bound to (action_id + canonical params hash), expiring in
300s. A call carrying a valid token proceeds: native actions dispatch;
catalog actions return the pointer disposition, noting the owning
tool's own gates still apply. Plain talk: for the credential class
this preview is a two-step handshake under the SAME D2 token (D2 is
the owner's decision: one token per host for all consumers). It guards
against accidental or replayed dispatch and writes an audit line; it
is NOT an out-of-band human approval, and no reader should treat it
as one. Corollary, on the record: through the break-glass ops a
token holder can mint the break-glass password, read it, and enable
an admin login — the per-host gateway token is a root-equivalent
credential on its host and is stored and handled accordingly.

Python standard library only, like factotum-mcp.
"""
import hashlib
import json
import os
import re
import secrets
import threading
import time
import uuid as uuidlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.2"
HERE = os.path.dirname(os.path.abspath(__file__))

MAX_RESULT_CHARS = 32768
PREVIEW_TTL_S = 300


def _load_env_file(path):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def load_config():
    """Config = this service's own .env, with process-environment
    overrides (the overrides exist for the test suite; production is
    configured by the .env alone)."""
    cfg = _load_env_file(os.path.join(HERE, ".env"))
    for key in ("GATEWAY_TOKEN", "GATEWAY_BIND", "GATEWAY_PORT",
                "GATEWAY_HOST", "FACTOTUM_SUPPORT_DIR", "GATEWAY_AUDIT_PATH",
                "GATEWAY_PREVIEW_TTL_S", "GATEWAY_TIMEOUT_DEFAULT_S",
                "GATEWAY_TIMEOUT_DISCOVERY_S", "GATEWAY_TIMEOUT_SERVICE_S",
                "GATEWAY_TIMEOUT_SERVICE_UPDATE_S",
                "GATEWAY_TIMEOUT_BREAKGLASS_ROTATE_S"):
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    return cfg


CONFIG = load_config()


def cfg(key, default=None):
    return CONFIG.get(key, default)


def support_dir():
    return cfg("FACTOTUM_SUPPORT_DIR",
               os.path.expanduser("~/Library/Application Support/Factotum"))


def registry_path():
    return os.path.join(support_dir(), "registry.json")


def capabilities_path():
    return os.path.join(support_dir(), "capabilities.json")


def inbox_dir():
    return os.path.join(support_dir(), "inbox")


def outbox_dir():
    return os.path.join(support_dir(), "outbox")


def audit_path():
    return cfg("GATEWAY_AUDIT_PATH",
               os.path.expanduser(
                   "~/Library/Application Support/FactotumGateway/audit.jsonl"))


def host_label():
    label = cfg("GATEWAY_HOST", "")
    if label:
        return label
    reg = read_json(registry_path())
    if reg and reg.get("host"):
        return str(reg["host"])
    import socket
    return socket.gethostname().split(".")[0].lower()


def timeout_for_op(op):
    if op == "service_update":
        return int(cfg("GATEWAY_TIMEOUT_SERVICE_UPDATE_S", "210"))
    if op == "discovery_run":
        return int(cfg("GATEWAY_TIMEOUT_DISCOVERY_S", "180"))
    if op == "breakglass_rotate":
        # The rotate's own chain (queued behind inbox work, plus
        # group reads, sudo -l, and the root helper) can exceed the
        # 90s default. 240s stays under the VM gateway client's 300s
        # HTTP cap. A timeout here is NOT proof the rotation did not
        # happen — see the distinct error in invoke_action.
        return int(cfg("GATEWAY_TIMEOUT_BREAKGLASS_ROTATE_S", "240"))
    return int(cfg("GATEWAY_TIMEOUT_DEFAULT_S", "90"))


def timeout_for_service(action):
    if action == "update":
        return int(cfg("GATEWAY_TIMEOUT_SERVICE_UPDATE_S", "210"))
    return int(cfg("GATEWAY_TIMEOUT_SERVICE_S", "60"))


# --- registry reading (mirrors factotum-mcp) ------------------------------

def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def load_registry():
    reg = read_json(registry_path())
    if reg is None:
        return None
    if not isinstance(reg, dict) or "apps" not in reg or "actions" not in reg:
        return None
    return reg


_CANDIDATE_RISK_TO_REGISTRY = {"read": "read", "local-write": "write",
                               "outward": "outward", "delete": "delete"}
_CANDIDATE_RISK_TO_OP = {"read": "read", "local-write": "update",
                         "outward": "update", "delete": "delete"}


def candidate_records():
    cap = read_json(capabilities_path())
    if not cap or not isinstance(cap, dict):
        return []
    return [c for c in cap.get("candidates", []) if isinstance(c, dict)]


def candidate_action_view(c):
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


def all_actions(reg):
    """Registry actions plus capability candidates not already in the
    registry (the shell folds candidates' section into registry.json,
    but the companion file is the candidate source of truth, exactly as
    factotum-mcp treats it)."""
    actions = list((reg or {}).get("actions", []))
    seen = {a.get("id") for a in actions}
    for c in candidate_records():
        view = candidate_action_view(c)
        if view.get("id") and view["id"] not in seen:
            actions.append(view)
            seen.add(view["id"])
    return actions


# --- app dispatch (file contract, mirrors factotum-mcp) -------------------

KNOWN_OPS = {"health", "registry_read", "discovery_run", "logs_recent",
             "restart_hint", "apps_running", "app_launch", "app_quit",
             "notes_recent", "notes_create", "mail_create_draft",
             "service_status", "service_start", "service_stop",
             "service_restart", "service_logs_recent", "service_update",
             "cleanup_dry_run", "cleanup_last_run",
             "packages_plan", "packages_apply", "packages_last_run",
             "notify", "payload_reload", "payload_rollback",
             # Shell 1.6.5: break-glass lifecycle ops.
             "breakglass_status", "breakglass_enable",
             "breakglass_disable", "breakglass_rotate"}


def op_for_action(action_id):
    if action_id in KNOWN_OPS:
        return action_id
    if action_id.startswith("factotum_"):
        rest = action_id[len("factotum_"):]
        if rest in KNOWN_OPS:
            return rest
    return None


def queue(payload):
    req_id = uuidlib.uuid4().hex[:12]
    payload = dict(payload)
    payload["id"] = req_id
    os.makedirs(inbox_dir(), exist_ok=True)
    tmp = os.path.join(inbox_dir(), "." + req_id + ".tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.rename(tmp, os.path.join(inbox_dir(), req_id + ".json"))
    return req_id


def wait_final(req_id, timeout_s):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        data = read_json(os.path.join(outbox_dir(), req_id + ".json"))
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
    out["request_id"] = req_id
    return out, req_id


# --- self actions answered locally when the app does not answer -----------
# Mirrors factotum-mcp's MCP_LOCAL fallbacks (same three action ids).

def _self_health(reg):
    support = support_dir()
    out = {"ok": True, "host": host_label(), "gateway_version": VERSION,
           "served_by": "gateway_fallback_app_not_answering",
           "registry_present": reg is not None, "support_dir": support,
           "inbox_depth": len([n for n in os.listdir(inbox_dir())
                               if n.endswith(".json")])
           if os.path.isdir(inbox_dir()) else 0,
           "outbox_depth": len([n for n in os.listdir(outbox_dir())
                                if n.endswith(".json")])
           if os.path.isdir(outbox_dir()) else 0}
    if reg is not None:
        out["registry_generated_at"] = reg.get("generated_at")
        out["apps_registered"] = len(reg.get("apps", []))
        out["actions_registered"] = len(reg.get("actions", []))
    return out


def _self_logs_recent(params):
    log = os.path.join(support_dir(), "factotum.log")
    n = int(params.get("lines", 50))
    if not os.path.exists(log):
        return {"ok": True, "lines": [],
                "note": "no factotum.log yet at " + log}
    with open(log, "rb") as f:
        data = f.read().decode("utf-8", "replace")
    lines = [ln for ln in data.splitlines() if ln.strip()]
    return {"ok": True, "lines": lines[-n:], "total_lines": len(lines)}


MCP_LOCAL = {
    "factotum_health": lambda reg, params: _self_health(reg),
    "factotum_registry_read": lambda reg, params: {"ok": True, "registry": reg},
    "factotum_logs_recent": lambda reg, params: _self_logs_recent(params),
}


# --- generated surface ------------------------------------------------------

def sanitize(name):
    """Action/service id -> tool-name fragment: dots and dashes (and any
    other non-word character) become underscores, consistently."""
    return re.sub(r"[^A-Za-z0-9_]", "_", str(name))


def is_gated(action):
    # D10 classes: delete/outward (decided Oct 19, 2026) plus the
    # credential class (security_class == "credential": break-glass
    # enable/rotate), which gates under the same preview-token rule.
    return action.get("op") == "delete" or \
        action.get("risk") in ("delete", "outward") or \
        action.get("security_class") == "credential"


def disposition_of(action):
    status = action.get("status")
    if status == "candidate":
        return "candidate: refused always, never dispatched"
    if is_gated(action):
        if status == "native":
            return "native, gated (D10 preview required before dispatch)"
        if status == "catalog":
            return ("catalog, gated (D10 preview required; with a valid "
                    "preview token the gateway returns the pointer "
                    "disposition and does not dispatch)")
    if status == "native":
        return "native: dispatched to Factotum.app via the inbox contract"
    if status == "catalog":
        return "catalog: pointer returned, owned by a sibling tool"
    if status == "unavailable":
        return "unavailable: errors, not dispatched"
    return "status %r: not dispatched" % status


class Surface:
    """The generated tool surface, regenerated whenever the registry
    file's mtime changes (checked on every tools/list and tools/call)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._mtime = None
        self._cap_mtime = None
        self.tools = []
        self.actions = {}       # tool name -> action record
        self.services = {}      # tool name -> service record
        self.generated_at = None
        self.registry_generated_at = None

    def _stat(self, path):
        try:
            return os.path.getmtime(path)
        except OSError:
            return None

    def refresh(self):
        mtime = self._stat(registry_path())
        cap_mtime = self._stat(capabilities_path())
        with self._lock:
            if mtime == self._mtime and cap_mtime == self._cap_mtime \
                    and self.tools:
                return
            reg = load_registry()
            self._build(reg)
            self._mtime = mtime
            self._cap_mtime = cap_mtime

    def _build(self, reg):
        tools = []
        self.actions = {}
        self.services = {}
        self.registry_generated_at = reg.get("generated_at") if reg else None
        actions = all_actions(reg) if reg else []
        for action in actions:
            aid = action.get("id")
            if not aid:
                continue
            name = sanitize(aid)
            desc_parts = [str(action.get("name") or aid),
                          "Factotum action %s (op=%s, risk=%s, status=%s)."
                          % (aid, action.get("op"), action.get("risk"),
                             action.get("status"))]
            extra = action.get("notes") or action.get("mechanism")
            if extra:
                desc_parts.append(str(extra))
            if is_gated(action):
                desc_parts.append(
                    "D10: delete/outward class; a plain call is refused "
                    "with a preview + single-use preview_token; call "
                    "again with preview_token to proceed.")
            tools.append({
                "name": name,
                "description": " ".join(desc_parts),
                "inputSchema": {"type": "object", "properties": {
                    "params": {"type": "object",
                               "description": "Free-form action params, "
                                              "passed through to the app "
                                              "exactly as factotum_invoke's "
                                              "params."},
                    "preview_token": {"type": "string",
                                      "description": "Single-use D10 "
                                                     "preview token from a "
                                                     "prior refused call."},
                    "consumer": {"type": "string",
                                 "description": "Caller-claimed identity "
                                                "for the audit log (or use "
                                                "the X-Consumer header)."}}},
            })
            self.actions[name] = action
        for svc in (reg or {}).get("services", []):
            sid = svc.get("id")
            if not sid:
                continue
            name = "service." + sanitize(sid)
            acts = list(svc.get("actions", []))
            tools.append({
                "name": name,
                "description": ("Manage registered service %s on this "
                                "host through Factotum.app (kind=%s, "
                                "desired_state=%s). action is one of the "
                                "service's registered actions."
                                % (sid, svc.get("kind"),
                                   svc.get("desired_state"))),
                "inputSchema": {"type": "object", "properties": {
                    "action": {"type": "string", "enum": acts},
                    "expected_sha256": {"type": "string",
                                        "description": "Required for "
                                                       "action=update."},
                    "expected_commit": {"type": "string",
                                        "description": "Required for "
                                                       "action=update."},
                    "preview_token": {"type": "string"},
                    "consumer": {"type": "string"}},
                    "required": ["action"]},
            })
            self.services[name] = svc
        tools.append({
            "name": "gateway.health",
            "description": "factotum-gateway self health: version, host, "
                           "bind, registry freshness, generated surface "
                           "counts, preview/audit posture. Read-only.",
            "inputSchema": {"type": "object", "properties": {}}})
        tools.append({
            "name": "gateway.catalog",
            "description": "The whole registry rendered as data: every "
                           "action (native, catalog, candidate, "
                           "unavailable) with its disposition, plus every "
                           "registered service. Read-only; dispatches "
                           "nothing.",
            "inputSchema": {"type": "object", "properties": {}}})
        self.tools = tools
        self.generated_at = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")


SURFACE = Surface()


# --- D10 preview tokens -----------------------------------------------------

def canonical_params_hash(params):
    blob = json.dumps(params or {}, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class PreviewStore:
    """Single-use preview tokens, held in gateway memory only.
    Bound to (action_id + canonical params hash); 300s expiry."""

    def __init__(self, ttl_s=PREVIEW_TTL_S):
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._tokens = {}

    def issue(self, action_id, params):
        token = secrets.token_hex(16)
        with self._lock:
            self._tokens[token] = {
                "action_id": action_id,
                "params_hash": canonical_params_hash(params),
                "expires_at": time.time() + self.ttl_s,
                "used": False}
        return token

    def validate_and_consume(self, token, action_id, params):
        """Returns (ok, reason). Consumes the token on success only."""
        with self._lock:
            rec = self._tokens.get(token)
            if rec is None:
                return False, "unknown preview_token"
            if rec["used"]:
                return False, "preview_token already used (single-use)"
            if time.time() > rec["expires_at"]:
                return False, "preview_token expired"
            if rec["action_id"] != action_id:
                return False, "preview_token was issued for a different action"
            if rec["params_hash"] != canonical_params_hash(params):
                return False, ("preview_token was issued for different "
                               "params (params hash mismatch)")
            rec["used"] = True
            return True, "ok"


PREVIEWS = PreviewStore(ttl_s=int(cfg("GATEWAY_PREVIEW_TTL_S",
                                      str(PREVIEW_TTL_S))))


def preview_description(action, params):
    aid = action.get("id")
    target = action.get("tool")
    pointer = action.get("pointer")
    if isinstance(pointer, dict):
        target = target or ("%s tool %s via %s"
                              % (pointer.get("mcp"), pointer.get("tool"),
                                 pointer.get("caller")))
    lines = ["Would run '%s' (%s) on host %s: op=%s, risk=%s, status=%s."
             % (action.get("name") or aid, aid, host_label(),
                action.get("op"), action.get("risk"), action.get("status"))]
    if action.get("mechanism"):
        mech = str(action["mechanism"])
        if not mech.endswith((".", "!", "?")):
            mech += "."
        lines.append("Mechanism: %s" % mech)
    if action.get("status") == "native":
        lines.append("Dispatch: Factotum.app inbox op '%s' with the "
                     "params below." % op_for_action(aid))
    elif target:
        lines.append("Target service/tool: %s" % target)
    if action.get("op") == "delete":
        lines.append("This is a DELETE-class action: its effect is not "
                     "revertible through the gateway.")
    if action.get("risk") == "outward":
        lines.append("This action has effects outward from this host.")
    if action.get("notes"):
        lines.append("Registry notes: %s" % action["notes"])
    lines.append("Params: %s" % json.dumps(params or {}, sort_keys=True))
    return " ".join(lines)


def preview_refusal(action, params, error=None):
    token = PREVIEWS.issue(action.get("id"), params)
    out = {"ok": False, "refused": True, "preview_required": True,
           "preview": {"action_id": action.get("id"),
                       "description": preview_description(action, params),
                       "op": action.get("op"), "risk": action.get("risk"),
                       "status": action.get("status"),
                       "params": params or {}},
           "preview_token": token,
           "preview_expires_in_s": PREVIEWS.ttl_s,
           "action": action}
    if error:
        out["error"] = error
    return out


# --- audit --------------------------------------------------------------------

_AUDIT_LOCK = threading.Lock()


def audit(record):
    try:
        path = audit_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _AUDIT_LOCK:
            with open(path, "a") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # audit must never take the gateway down


# --- tool implementations -----------------------------------------------------

def gateway_health():
    SURFACE.refresh()
    reg = load_registry()
    return {"ok": True, "service": "factotum-gateway", "version": VERSION,
            "host": host_label(),
            "bind": cfg("GATEWAY_BIND"), "port": int(cfg("GATEWAY_PORT", "7902")),
            "token_configured": bool(cfg("GATEWAY_TOKEN")),
            "registry_present": reg is not None,
            "registry_generated_at": SURFACE.registry_generated_at,
            "registry_path": registry_path(),
            "surface_generated_at": SURFACE.generated_at,
            "tools": len(SURFACE.tools),
            "actions": len(SURFACE.actions),
            "services": len(SURFACE.services),
            "audit_path": audit_path(),
            "preview_ttl_s": PREVIEWS.ttl_s}


def gateway_catalog():
    SURFACE.refresh()
    reg = load_registry()
    if reg is None:
        return {"ok": False,
                "error": "registry missing or unreadable at " + registry_path()}
    actions = []
    for name, action in sorted(SURFACE.actions.items(),
                               key=lambda kv: kv[1].get("id") or ""):
        actions.append({
            "id": action.get("id"), "tool": name,
            "name": action.get("name"), "op": action.get("op"),
            "risk": action.get("risk"), "status": action.get("status"),
            "mechanism": action.get("mechanism"),
            "gated": is_gated(action),
            "disposition": disposition_of(action),
            "pointer": action.get("pointer") or action.get("tool")})
    services = [{"id": s.get("id"), "tool": "service." + sanitize(s.get("id")),
                 "launchd_label": s.get("launchd_label"),
                 "kind": s.get("kind"),
                 "desired_state": s.get("desired_state"),
                 "actions": s.get("actions", [])}
                for s in reg.get("services", [])]
    return {"ok": True, "host": host_label(),
            "generated_at": reg.get("generated_at"),
            "surface_generated_at": SURFACE.generated_at,
            "counts": {"actions": len(actions), "services": len(services),
                       "native": sum(1 for a in actions
                                     if a["status"] == "native"),
                       "catalog": sum(1 for a in actions
                                      if a["status"] == "catalog"),
                       "candidate": sum(1 for a in actions
                                        if a["status"] == "candidate"),
                       "unavailable": sum(1 for a in actions
                                          if a["status"] == "unavailable")},
            "actions": actions, "services": services}


def invoke_action(action, args):
    """Returns (result_dict, outcome). Mirrors factotum-mcp tool_invoke's
    refusal semantics, with the D10 preview gate in place of the old
    flat refusal for delete/outward classes."""
    action_id = action.get("id")
    params = args.get("params") or {}
    preview_token = args.get("preview_token")
    status = action.get("status")
    # Candidate refuses before every other gate, always.
    if status == "candidate":
        return ({"ok": False, "refused": True,
                 "error": ("action %s is a candidate (status=candidate) "
                           "and is not invocable" % action_id),
                 "reason": ("candidate actions are researched proposals "
                            "recorded for review; promotion to a live "
                            "action is a human/operator decision, per action, "
                            "and never happens through invoke"),
                 "action": action}, "refused")
    if is_gated(action):
        if not preview_token:
            return preview_refusal(action, params), "refused"
        ok, reason = PREVIEWS.validate_and_consume(preview_token,
                                                   action_id, params)
        if not ok:
            return (preview_refusal(
                action, params,
                error="preview_token rejected: %s" % reason),
                "refused")
        # Valid preview: proceed by class. The token is consumed.
        if status == "catalog":
            return ({"ok": True, "invoked": False, "catalog": True,
                     "preview_consumed": True,
                     "note": ("preview accepted. Catalog action: owned by "
                              "a sibling tool, not Factotum; the gateway "
                              "does not dispatch it, and the owning "
                              "tool's own gates still apply. Use the "
                              "pointer."),
                     "pointer": action.get("pointer") or action.get("tool"),
                     "action": action}, "ok")
        if status == "unavailable":
            return ({"ok": False,
                     "error": "action registered as unavailable on this host",
                     "preview_consumed": True, "action": action}, "error")
        # native: fall through to dispatch below.
    elif status == "catalog":
        return ({"ok": True, "invoked": False, "catalog": True,
                 "note": ("catalog action: owned by a sibling tool, not "
                          "Factotum. Use the pointer."),
                 "pointer": action.get("pointer") or action.get("tool"),
                 "action": action}, "ok")
    elif status == "unavailable":
        return ({"ok": False,
                 "error": "action registered as unavailable on this host",
                 "action": action}, "error")
    elif status != "native":
        return ({"ok": False,
                 "error": "unknown action status: %r" % status,
                 "action": action}, "error")
    op = op_for_action(action_id)
    if op is None:
        return ({"ok": False,
                 "error": ("native action %s has no app op mapping"
                           % action_id),
                 "action": action}, "error")
    timeout = timeout_for_op(op)
    result, req_id = app_call({"op": op, "params": params}, timeout)
    if result is None and op == "breakglass_rotate":
        # The app may have completed the rotation after this wait
        # gave up; the password (if any) stays in the outbox record
        # under the payload's reaper backstop. Never let a caller
        # read a bare timeout as "rotation did not happen".
        return ({"ok": False, "error_kind": "rotate_outcome_unknown",
                 "error": ("timed out after %ds waiting for "
                           "breakglass_rotate; the rotation may "
                           "still have completed on the host. Check "
                           "breakglass_status last_rotated_at before "
                           "any retry; do NOT retry blindly."
                           % timeout),
                 "request_id": req_id, "action": action}, "timeout")
    if result is not None and op == "breakglass_rotate":
        # The result carries the rotated password. Delete its
        # outbox record NOW: the shell's housekeeping sweep and
        # the payload reaper are backstops and must never be the
        # primary deleter of a live credential. One retry, and a
        # failure is flagged on the result rather than swallowed —
        # a silent unlink failure would quietly restore the
        # at-rest posture this deletion exists to prevent.
        deleted = False
        for _attempt in range(2):
            try:
                os.unlink(os.path.join(outbox_dir(), req_id + ".json"))
                deleted = True
                break
            except FileNotFoundError:
                deleted = True
                break
            except OSError:
                time.sleep(0.2)
        if not deleted:
            result["outbox_delete_failed"] = True
    if result is None:
        if action_id in MCP_LOCAL:
            reg = load_registry()
            out = MCP_LOCAL[action_id](reg, params)
            out["request_id"] = req_id
            out["action"] = action
            return out, "ok"
        return ({"ok": False, "error_kind": "app_not_answering",
                 "error": ("Factotum.app did not answer invoke of %s "
                           "(op %s) in %ds; it may not be running yet on "
                           "this host" % (action_id, op, timeout)),
                 "request_id": req_id, "action": action}, "timeout")
    result.setdefault("invoked", True)
    result["action"] = action
    return result, "ok"


def invoke_service(svc, args):
    service_id = svc.get("id")
    action = args.get("action") or ""
    if action not in ("status", "start", "stop", "restart",
                      "logs_recent", "update"):
        return ({"ok": False, "error": "unknown action: " + action},
                "error")
    if action not in svc.get("actions", []):
        return ({"ok": False,
                 "error": ("action %s not registered for service %s "
                           "(registered: %s)"
                           % (action, service_id,
                              ", ".join(svc.get("actions", []))))},
                "error")
    params = {"service_id": service_id}
    if action == "update":
        params["expected_sha256"] = args.get("expected_sha256")
        params["expected_commit"] = args.get("expected_commit")
    timeout = timeout_for_service(action)
    result, req_id = app_call({"op": "service_" + action,
                               "params": params}, timeout)
    if result is None:
        return ({"ok": False, "error_kind": "app_not_answering",
                 "error": ("Factotum.app did not answer service %s %s; "
                           "service management dispatches through the app"
                           % (action, service_id)),
                 "request_id": req_id, "service": svc}, "timeout")
    result.setdefault("invoked", True)
    result["service"] = svc
    return result, "ok"


def dispatch_tool(name, args, consumer):
    """Returns (result_dict, outcome, action_id, service_id, preview_flag)."""
    SURFACE.refresh()
    args = args or {}
    if name == "gateway.health":
        return gateway_health(), "ok", None, None, False
    if name == "gateway.catalog":
        result = gateway_catalog()
        return result, ("ok" if result.get("ok") else "error"), None, None, False
    if name in SURFACE.actions:
        action = SURFACE.actions[name]
        result, outcome = invoke_action(action, args)
        preview = bool(args.get("preview_token")) or \
            bool(result.get("preview_required"))
        return result, outcome, action.get("id"), None, preview
    if name in SURFACE.services:
        svc = SURFACE.services[name]
        result, outcome = invoke_service(svc, args)
        return result, outcome, None, svc.get("id"), \
            bool(args.get("preview_token"))
    return ({"ok": False, "error": "unknown tool " + name},
            "error", None, None, False)


def truncate_text(text):
    """Returns (text_to_send, full_char_count). Caps a single tool
    result's text at MAX_RESULT_CHARS with a visible marker."""
    if len(text) <= MAX_RESULT_CHARS:
        return text, len(text)
    marker = ("\n[truncated by factotum-gateway: full result was %d chars "
              "(%d bytes); showing the first %d chars]"
              % (len(text), len(text.encode("utf-8")), MAX_RESULT_CHARS))
    return text[:MAX_RESULT_CHARS] + marker, len(text)


# --- HTTP / MCP plumbing -------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _authorized(self):
        token = cfg("GATEWAY_TOKEN", "")
        if not token:
            return False
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        return secrets.compare_digest(auth[len("Bearer "):], token)

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

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "service": "factotum-gateway",
                             "version": VERSION, "host": host_label()})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/mcp", "/"):
            self._send(404, {"error": "not found"})
            return
        if not self._authorized():
            self._send(403, {"error": "forbidden: missing or invalid "
                                      "bearer token"})
            return
        try:
            body = json.loads(self.rfile.read(
                int(self.headers.get("Content-Length", 0))))
        except Exception:
            self._send(400, {"error": "bad json"})
            return
        method = body.get("method", "")
        rid = body.get("id")
        if method == "initialize":
            self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "factotum-gateway",
                               "version": VERSION}}},
                session="gateway-session")
            return
        if method.startswith("notifications/"):
            self._send(202, None)
            return
        if method == "tools/list":
            SURFACE.refresh()
            self._send(200, {"jsonrpc": "2.0", "id": rid,
                             "result": {"tools": SURFACE.tools}})
            return
        if method == "tools/call":
            params = body.get("params", {})
            name = params.get("name", "")
            args = params.get("arguments", {}) or {}
            consumer = self.headers.get("X-Consumer") or \
                args.get("consumer") or "unknown"
            started = time.time()
            try:
                result, outcome, action_id, service_id, preview = \
                    dispatch_tool(name, args, consumer)
            except Exception as e:
                result, outcome, action_id, service_id, preview = \
                    ({"ok": False, "error": "tool failed: %s" % e},
                     "error", None, None, False)
            full_text = json.dumps(result, indent=1, ensure_ascii=False)
            text, _full_chars = truncate_text(full_text)
            duration_ms = int((time.time() - started) * 1000)
            audit({"ts": datetime.now(timezone.utc).strftime(
                       "%Y-%m-%dT%H:%M:%SZ"),
                   "host": host_label(), "consumer": consumer,
                   "tool": name, "action_id": action_id,
                   "service_id": service_id, "outcome": outcome,
                   "duration_ms": duration_ms,
                   "result_bytes": len(full_text.encode("utf-8")),
                   "preview": preview})
            self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": text}],
                "isError": outcome == "error"}})
            return
        self._send(200, {"jsonrpc": "2.0", "id": rid,
                         "error": {"code": -32601,
                                   "message": "unknown method"}})


def make_server():
    bind = cfg("GATEWAY_BIND", "")
    if not bind:
        raise SystemExit("GATEWAY_BIND is not set in the gateway .env; "
                         "the gateway binds the host's tailnet IP only "
                         "(D12) and refuses to guess an interface")
    if not cfg("GATEWAY_TOKEN"):
        raise SystemExit("GATEWAY_TOKEN is not set in the gateway .env; "
                         "refusing to serve unauthenticated")
    port = int(cfg("GATEWAY_PORT", "7902"))
    return ThreadingHTTPServer((bind, port), Handler)


if __name__ == "__main__":
    SURFACE.refresh()
    make_server().serve_forever()
