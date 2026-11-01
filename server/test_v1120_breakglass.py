"""v1.12.0 break-glass lifecycle tests (shell 1.6.5 ops).

The four breakglass_* natives arrive with payload 1.6.0-p9 /
shell 1.6.5. This plane's contract:

- all four action ids map to same-named shell ops;
- enable + rotate carry security_class=credential and are REFUSED
  here (the D10 preview-token rule lives in factotum-gateway; this
  plane has no preview flow, so it refuses and names the path);
- status + disable are ungated and dispatch to the app.
"""
import unittest
from unittest import mock

import server


def _action(aid, op, risk, **kw):
    a = {"id": aid, "app_bundle_id": "io.example.factotum",
         "name": aid, "op": op, "risk": risk, "status": "native",
         "mechanism": "inbox protocol op=" + aid}
    a.update(kw)
    return a


REGISTRY = {"host": "fixturehost", "actions": [
    _action("breakglass_status", "read", "read"),
    _action("breakglass_enable", "update", "write",
            security_class="credential"),
    _action("breakglass_disable", "update", "write"),
    _action("breakglass_rotate", "update", "write",
            security_class="credential"),
]}


class OpMappingTest(unittest.TestCase):
    def test_all_four_ids_map_to_shell_ops(self):
        for aid in ("breakglass_status", "breakglass_enable",
                    "breakglass_disable", "breakglass_rotate"):
            self.assertEqual(server.op_for_action(aid), aid)


class CredentialGateTest(unittest.TestCase):
    def setUp(self):
        self._load = mock.patch.object(
            server, "load_registry", return_value=REGISTRY)
        self._load.start()
        self.addCleanup(self._load.stop)

    def test_credential_actions_refused_without_dispatch(self):
        with mock.patch.object(
                server, "app_call",
                side_effect=AssertionError("must not dispatch")):
            for aid in ("breakglass_enable", "breakglass_rotate"):
                out = server.tool_invoke({"action_id": aid})
                self.assertFalse(out["ok"], aid)
                self.assertTrue(out["refused"], aid)
                self.assertIn("factotum-gateway", out["error"])

    def test_status_and_disable_dispatch(self):
        with mock.patch.object(
                server, "app_call",
                return_value=({"ok": True, "state": "disabled"}, "req1")):
            for aid in ("breakglass_status", "breakglass_disable"):
                out = server.tool_invoke({"action_id": aid})
                self.assertTrue(out["ok"], aid)
                self.assertEqual(out["state"], "disabled")


if __name__ == "__main__":
    unittest.main()
