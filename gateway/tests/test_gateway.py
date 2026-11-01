#!/usr/bin/env python3
"""factotum-gateway test suite (stdlib unittest, no network beyond
loopback, no real Factotum.app: a fake app answers the inbox/outbox
file contract).

Run:  python3 -m unittest discover -s gateway/tests -v
  or: python3 gateway/tests/test_gateway.py

Covers: bearer auth, generated surface vs registry (zero missing /
zero extra), catalog/candidate/unavailable dispositions, the D10
preview flow (refusal, concrete preview, token mechanics: param-hash
binding, single-use, expiry), native dispatch WITH a valid preview
token against a harmless fixture op, service dispatch, per-tool
timeout, result truncation, audit lines, and surface regeneration on
registry mtime change without a restart.
"""
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
GATEWAY_DIR = os.path.dirname(HERE)
sys.path.insert(0, GATEWAY_DIR)

TOKEN = "test-token-not-a-real-secret"

REGISTRY = {
    "host": "fixturehost",
    "generated_at": "2026-10-19T00:00:00Z",
    "apps": [],
    "actions": [
        {"id": "factotum_health", "name": "Factotum health",
         "op": "read", "risk": "read", "status": "native",
         "mechanism": "inbox protocol op=health"},
        {"id": "factotum_logs_recent", "name": "Recent logs",
         "op": "read", "risk": "read", "status": "native",
         "mechanism": "inbox protocol op=logs_recent"},
        {"id": "apps_running", "name": "Running apps",
         "op": "read", "risk": "read", "status": "native",
         "mechanism": "inbox protocol op=apps_running"},
        # Fixture native DELETE action: gated by D10, but its id maps
        # to the harmless cleanup_last_run app op, so dispatch with a
        # valid token exercises the real path against a no-op fixture.
        {"id": "cleanup_last_run", "name": "Fixture delete (harmless op)",
         "op": "delete", "risk": "delete", "status": "native",
         "mechanism": "inbox protocol op=cleanup_last_run",
         "notes": "Fixture: delete-class record, read-class app op."},
        {"id": "shortcuts.list", "name": "List shortcuts",
         "op": "read", "risk": "read", "status": "catalog",
         "mechanism": "shortcuts-mcp shortcuts_list",
         "pointer": {"mcp": "shortcuts-mcp", "tool": "shortcuts_list",
                     "caller": "~/bin/shortcuts-mcp-call"}},
        {"id": "shortcuts.disarm_blink", "name": "Disarm Blink",
         "op": "update", "risk": "outward", "status": "catalog",
         "mechanism": "shortcuts-mcp shortcuts_run {name: 'Disarm Blink'}",
         "pointer": {"mcp": "shortcuts-mcp", "tool": "shortcuts_run",
                     "caller": "~/bin/shortcuts-mcp-call"}},
        {"id": "photopull.staging_clean", "name": "Clean staged files",
         "op": "delete", "risk": "delete", "status": "catalog",
         "mechanism": "photopull-mcp photopull_staging action=clean",
         "pointer": {"mcp": "photopull-mcp", "tool": "photopull_staging",
                     "caller": "~/bin/photopull-mcp-call"}},
        {"id": "photopull.photopull_fetch", "name": "Fetch photos",
         "op": "create", "risk": "write", "status": "unavailable",
         "mechanism": "photopull-mcp photopull_fetch"},
        {"id": "nimolo.future_thing", "name": "Future thing",
         "op": "read", "risk": "read", "status": "candidate",
         "mechanism": "researched surface"},
        # Break-glass credential class (gateway 1.0.1): native,
        # write-risk, gated purely by security_class. Maps to the
        # harmless breakglass_rotate app op (FakeApp echoes).
        {"id": "breakglass_rotate", "name": "Rotate break-glass",
         "op": "update", "risk": "write", "status": "native",
         "security_class": "credential",
         "mechanism": "inbox protocol op=breakglass_rotate"},
        {"id": "breakglass_status", "name": "Break-glass status",
         "op": "read", "risk": "read", "status": "native",
         "mechanism": "inbox protocol op=breakglass_status"},
    ],
    "services": [
        {"id": "factotum", "launchd_label": "io.example.factotum",
         "kind": "app", "desired_state": "running",
         "actions": ["status", "start", "stop", "restart", "logs_recent"]},
        {"id": "bumblebee-baseline", "launchd_label": "io.example.bumblebee",
         "kind": "daemon", "desired_state": "running",
         "actions": ["status", "start", "stop", "restart", "logs_recent",
                     "update"]},
    ],
}

CAPABILITIES = {"candidates": [
    {"id": "screens.connect_adhoc", "app_bundle_id": "com.example.screens",
     "description": "Connect ad hoc", "surface": "screens UI",
     "risk_class": "outward", "evidence": "fixture", "confidence": "low"}]}


class FakeApp(threading.Thread):
    """Answers inbox requests in the support dir like Factotum.app."""

    def __init__(self, support, big=False, hang_ops=()):
        super().__init__(daemon=True)
        self.support = support
        self.big = big
        self.hang_ops = set(hang_ops)
        self.seen = []

    def run(self):
        inbox = os.path.join(self.support, "inbox")
        outbox = os.path.join(self.support, "outbox")
        os.makedirs(inbox, exist_ok=True)
        os.makedirs(outbox, exist_ok=True)
        while True:
            try:
                for name in os.listdir(inbox):
                    if not name.endswith(".json") or name.startswith("."):
                        continue
                    path = os.path.join(inbox, name)
                    try:
                        with open(path) as f:
                            req = json.load(f)
                    except Exception:
                        continue
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    self.seen.append(req)
                    if req.get("op") in self.hang_ops:
                        continue  # never answers: drives the timeout path
                    result = {"ok": True, "op": req.get("op"),
                              "echo_params": req.get("params")}
                    if req.get("op") == "breakglass_rotate":
                        # Synthetic marker value (not a credential):
                        # lets the deletion test assert the value
                        # reaches neither disk nor the audit log.
                        result["password"] = "fixture-marker-not-a-credential"
                        result["user"] = "breakglass"
                        result["validated"] = True
                    if self.big:
                        result["blob"] = "x" * 100000
                    out = os.path.join(outbox, req["id"] + ".json")
                    with open(out + ".tmp", "w") as f:
                        json.dump(result, f)
                    os.rename(out + ".tmp", out)
            except Exception:
                pass
            time.sleep(0.05)


class GatewayTestCase(unittest.TestCase):
    big = False
    hang_ops = ()
    extra_env = {}

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="gateway-test-")
        cls.support = os.path.join(cls.tmp, "Factotum")
        os.makedirs(os.path.join(cls.support, "inbox"))
        os.makedirs(os.path.join(cls.support, "outbox"))
        cls.registry_file = os.path.join(cls.support, "registry.json")
        with open(cls.registry_file, "w") as f:
            json.dump(REGISTRY, f)
        with open(os.path.join(cls.support, "capabilities.json"), "w") as f:
            json.dump(CAPABILITIES, f)
        cls.audit_file = os.path.join(cls.tmp, "audit.jsonl")
        env = {"GATEWAY_TOKEN": TOKEN, "GATEWAY_BIND": "127.0.0.1",
               "GATEWAY_PORT": "0", "GATEWAY_HOST": "fixturehost",
               "FACTOTUM_SUPPORT_DIR": cls.support,
               "GATEWAY_AUDIT_PATH": cls.audit_file,
               "GATEWAY_TIMEOUT_DEFAULT_S": "3"}
        env.update(cls.extra_env)
        cls._saved_env = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        import importlib
        import server as gw
        importlib.reload(gw)
        cls.gw = gw
        cls.app = FakeApp(cls.support, big=cls.big, hang_ops=cls.hang_ops)
        cls.app.start()
        cls.httpd = gw.make_server()
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d/mcp" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        for k, v in cls._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def post(self, payload, token=TOKEN, consumer=None):
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if consumer:
            headers["X-Consumer"] = consumer
        req = urllib.request.Request(self.base,
                                     data=json.dumps(payload).encode(),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def rpc(self, method, params=None, **kw):
        status, body = self.post({"jsonrpc": "2.0", "id": 1,
                                  "method": method, "params": params or {}},
                                 **kw)
        assert status == 200, (status, body)
        return body["result"]

    def call(self, tool, args=None, **kw):
        result = self.rpc("tools/call",
                          {"name": tool, "arguments": args or {}}, **kw)
        return json.loads(result["content"][0]["text"]), result


class TestAuth(GatewayTestCase):
    def test_no_token_rejected(self):
        status, _ = self.post({"jsonrpc": "2.0", "id": 1,
                               "method": "tools/list", "params": {}},
                              token=None)
        self.assertIn(status, (401, 403))

    def test_wrong_token_rejected(self):
        status, _ = self.post({"jsonrpc": "2.0", "id": 1,
                               "method": "tools/list", "params": {}},
                              token="not-the-token")
        self.assertIn(status, (401, 403))

    def test_correct_token_works(self):
        result = self.rpc("tools/list")
        self.assertIn("tools", result)


class TestSurface(GatewayTestCase):
    def test_surface_matches_registry_exactly(self):
        tools = self.rpc("tools/list")["tools"]
        names = {t["name"] for t in tools}
        expected = {self.gw.sanitize(a["id"]) for a in REGISTRY["actions"]}
        expected |= {self.gw.sanitize(c["id"])
                     for c in CAPABILITIES["candidates"]}
        expected |= {"service." + self.gw.sanitize(s["id"])
                     for s in REGISTRY["services"]}
        expected |= {"gateway.health", "gateway.catalog"}
        self.assertEqual(names, expected)

    def test_service_tool_enum(self):
        tools = {t["name"]: t for t in self.rpc("tools/list")["tools"]}
        enum = tools["service.bumblebee_baseline"]["inputSchema"][
            "properties"]["action"]["enum"]
        self.assertEqual(enum, ["status", "start", "stop", "restart",
                                "logs_recent", "update"])

    def test_catalog_lists_everything(self):
        result, _ = self.call("gateway.catalog")
        self.assertTrue(result["ok"])
        ids = {a["id"] for a in result["actions"]}
        self.assertEqual(ids, {a["id"] for a in REGISTRY["actions"]} |
                         {"screens.connect_adhoc"})
        self.assertEqual(result["counts"]["candidate"], 2)

    def test_health_tool(self):
        result, _ = self.call("gateway.health")
        self.assertTrue(result["ok"])
        self.assertEqual(result["host"], "fixturehost")
        self.assertFalse(result.get("token") == TOKEN)

    def test_regeneration_on_registry_change(self):
        reg = json.loads(json.dumps(REGISTRY))
        reg["actions"].append({"id": "notes_recent", "name": "Notes",
                               "op": "read", "risk": "read",
                               "status": "native",
                               "mechanism": "inbox protocol op=notes_recent"})
        with open(self.registry_file, "w") as f:
            json.dump(reg, f)
        # Force a distinct mtime even on coarse filesystems.
        st = os.stat(self.registry_file)
        os.utime(self.registry_file, (st.st_atime, st.st_mtime + 2))
        names = {t["name"] for t in self.rpc("tools/list")["tools"]}
        self.assertIn("notes_recent", names)
        with open(self.registry_file, "w") as f:
            json.dump(REGISTRY, f)
        st = os.stat(self.registry_file)
        os.utime(self.registry_file, (st.st_atime, st.st_mtime + 2))


class TestDispositions(GatewayTestCase):
    def test_native_dispatch(self):
        result, _ = self.call("factotum_health")
        self.assertTrue(result["ok"])
        self.assertEqual(result["op"], "health")

    def test_catalog_pointer_not_dispatched(self):
        before = len(self.app.seen)
        result, _ = self.call("shortcuts_list")
        self.assertTrue(result["ok"])
        self.assertTrue(result["catalog"])
        self.assertFalse(result["invoked"])
        self.assertEqual(len(self.app.seen), before)

    def test_candidate_refuses_always(self):
        result, _ = self.call("nimolo_future_thing")
        self.assertFalse(result["ok"])
        self.assertTrue(result["refused"])
        # Candidate check precedes the D10 gate: no preview is issued
        # even for an outward candidate.
        result2, _ = self.call("screens_connect_adhoc")
        self.assertTrue(result2["refused"])
        self.assertNotIn("preview_token", result2)

    def test_unavailable_errors(self):
        result, _ = self.call("photopull_photopull_fetch")
        self.assertFalse(result["ok"])
        self.assertIn("unavailable", result["error"])

    def test_service_dispatch(self):
        result, _ = self.call("service.factotum", {"action": "status"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["op"], "service_status")
        self.assertEqual(result["echo_params"]["service_id"], "factotum")

    def test_service_unregistered_action(self):
        result, _ = self.call("service.factotum", {"action": "update"})
        self.assertFalse(result["ok"])
        self.assertIn("not registered", result["error"])


class TestPreviewFlow(GatewayTestCase):
    def _preview_for(self, tool, params):
        result, _ = self.call(tool, {"params": params})
        self.assertFalse(result["ok"])
        self.assertTrue(result["preview_required"])
        self.assertIn("preview", result)
        self.assertIn("preview_token", result)
        return result

    def test_catalog_outward_refused_with_concrete_preview(self):
        r = self._preview_for("shortcuts_disarm_blink", {})
        desc = r["preview"]["description"]
        self.assertIn("Disarm Blink", desc)
        self.assertIn("shortcuts_run", desc)
        self.assertIn("Params:", desc)

    def test_catalog_delete_refused(self):
        r = self._preview_for("photopull_staging_clean", {})
        self.assertEqual(r["preview"]["op"], "delete")

    def test_catalog_gated_with_token_returns_pointer_no_dispatch(self):
        before = len(self.app.seen)
        r = self._preview_for("shortcuts_disarm_blink", {})
        result, _ = self.call("shortcuts_disarm_blink",
                              {"params": {},
                               "preview_token": r["preview_token"]})
        self.assertTrue(result["ok"])
        self.assertTrue(result["catalog"])
        self.assertTrue(result["preview_consumed"])
        self.assertIn("own gates still apply", result["note"])
        self.assertEqual(len(self.app.seen), before)  # never dispatched

    def test_native_delete_dispatches_with_valid_token(self):
        r = self._preview_for("cleanup_last_run", {"fixture": True})
        result, _ = self.call("cleanup_last_run",
                              {"params": {"fixture": True},
                               "preview_token": r["preview_token"]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["op"], "cleanup_last_run")
        self.assertEqual(result["echo_params"], {"fixture": True})

    def test_token_bound_to_params_hash(self):
        r = self._preview_for("cleanup_last_run", {"a": 1})
        result, _ = self.call("cleanup_last_run",
                              {"params": {"a": 2},
                               "preview_token": r["preview_token"]})
        self.assertFalse(result["ok"])
        self.assertTrue(result["preview_required"])
        self.assertIn("different params", result["error"])

    def test_token_single_use(self):
        r = self._preview_for("cleanup_last_run", {})
        first, _ = self.call("cleanup_last_run",
                             {"params": {},
                              "preview_token": r["preview_token"]})
        self.assertTrue(first["ok"])
        second, _ = self.call("cleanup_last_run",
                              {"params": {},
                               "preview_token": r["preview_token"]})
        self.assertFalse(second["ok"])
        self.assertIn("already used", second["error"])

    def test_token_expiry(self):
        store = self.gw.PreviewStore(ttl_s=1)
        token = store.issue("cleanup_last_run", {})
        ok, _ = store.validate_and_consume(token, "cleanup_last_run", {})
        self.assertTrue(ok)
        token2 = store.issue("cleanup_last_run", {})
        time.sleep(1.2)
        ok, reason = store.validate_and_consume(token2, "cleanup_last_run",
                                                {})
        self.assertFalse(ok)
        self.assertIn("expired", reason)

    def test_token_wrong_action(self):
        store = self.gw.PreviewStore(ttl_s=60)
        token = store.issue("cleanup_last_run", {})
        ok, reason = store.validate_and_consume(
            token, "photopull.staging_clean", {})
        self.assertFalse(ok)
        self.assertIn("different action", reason)


class TestCredentialGate(GatewayTestCase):
    def test_credential_native_refused_with_preview_no_dispatch(self):
        before = len(self.app.seen)
        result, _ = self.call("breakglass_rotate", {"params": {}})
        self.assertFalse(result["ok"])
        self.assertTrue(result["preview_required"])
        self.assertIn("preview_token", result)
        self.assertEqual(result["preview"]["action_id"],
                         "breakglass_rotate")
        self.assertEqual(len(self.app.seen), before)

    def test_credential_native_dispatches_with_valid_token(self):
        r, _ = self.call("breakglass_rotate", {"params": {}})
        result, _ = self.call(
            "breakglass_rotate",
            {"params": {}, "preview_token": r["preview_token"]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["op"], "breakglass_rotate")

    def test_ungated_sibling_dispatches_without_preview(self):
        result, _ = self.call("breakglass_status")
        self.assertTrue(result["ok"])
        self.assertEqual(result["op"], "breakglass_status")

    def test_rotate_timeout_mapping(self):
        self.assertEqual(self.gw.timeout_for_op("breakglass_rotate"), 240)

    def test_rotate_outbox_record_deleted_after_read(self):
        # The rotate result carries a live credential: the gateway
        # must unlink its outbox record before responding, not
        # leave deletion to the payload's cadence-bound reaper.
        outbox = os.path.join(self.support, "outbox")
        before = set(os.listdir(outbox))
        r, _ = self.call("breakglass_rotate", {"params": {}})
        result, _ = self.call(
            "breakglass_rotate",
            {"params": {}, "preview_token": r["preview_token"]})
        self.assertTrue(result["ok"])
        after = set(os.listdir(outbox))
        self.assertEqual(after - before, set())
        # The marker value reaches the caller in the result (its
        # one legitimate copy) and NOWHERE else: no outbox file
        # may contain it, and the audit log never sees it.
        self.assertEqual(result["password"],
                         "fixture-marker-not-a-credential")
        for name in os.listdir(outbox):
            with open(os.path.join(outbox, name)) as f:
                self.assertNotIn("fixture-marker-not-a-credential",
                                 f.read())
        with open(self.audit_file) as f:
            self.assertNotIn("fixture-marker-not-a-credential",
                             f.read())


class TestRotateTimeout(GatewayTestCase):
    hang_ops = ("breakglass_rotate",)
    extra_env = {"GATEWAY_TIMEOUT_BREAKGLASS_ROTATE_S": "1"}

    def test_rotate_timeout_reports_outcome_unknown(self):
        r, _ = self.call("breakglass_rotate", {"params": {}})
        result, _ = self.call(
            "breakglass_rotate",
            {"params": {}, "preview_token": r["preview_token"]})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_kind"], "rotate_outcome_unknown")
        self.assertIn("last_rotated_at", result["error"])


class TestTruncation(GatewayTestCase):
    big = True

    def test_result_truncated_with_marker_and_full_count(self):
        result = self.rpc("tools/call",
                          {"name": "factotum_health", "arguments": {}})
        text = result["content"][0]["text"]
        self.assertLessEqual(len(text),
                             self.gw.MAX_RESULT_CHARS + 200)
        self.assertIn("truncated by factotum-gateway", text)
        self.assertIn("full result was", text)


class TestTimeout(GatewayTestCase):
    hang_ops = ("apps_running",)

    def test_app_not_answering_times_out(self):
        started = time.time()
        result, raw = self.call("apps_running")
        elapsed = time.time() - started
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_kind"], "app_not_answering")
        self.assertLess(elapsed, 30)


class TestAudit(GatewayTestCase):
    def test_audit_lines_have_required_fields(self):
        self.call("factotum_health", consumer="audit-test")
        self.call("shortcuts_disarm_blink", {"params": {}})
        lines = []
        deadline = time.time() + 5
        while time.time() < deadline:
            if os.path.exists(self.audit_file):
                with open(self.audit_file) as f:
                    lines = [json.loads(ln) for ln in f if ln.strip()]
            if len(lines) >= 2:
                break
            time.sleep(0.1)
        mine = [ln for ln in lines if ln.get("consumer") == "audit-test"]
        self.assertTrue(mine)
        rec = mine[-1]
        for field in ("ts", "host", "consumer", "tool", "action_id",
                      "outcome", "duration_ms", "result_bytes", "preview"):
            self.assertIn(field, rec)
        self.assertEqual(rec["action_id"], "factotum_health")
        gated = [ln for ln in lines
                 if ln.get("action_id") == "shortcuts.disarm_blink"]
        self.assertTrue(gated)
        self.assertEqual(gated[-1]["outcome"], "refused")
        self.assertTrue(gated[-1]["preview"])


if __name__ == "__main__":
    unittest.main()
