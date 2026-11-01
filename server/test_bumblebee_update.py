import unittest
from unittest.mock import patch

import server


class BumblebeeUpdateTest(unittest.TestCase):
    def setUp(self):
        self.registry = {"services": [
            {"id": "bumblebee-baseline", "actions": ["status", "update"]},
            {"id": "factotum", "actions": ["status"]},
        ]}

    def test_update_forwards_pinned_artifact_to_app(self):
        with patch.object(server, "load_registry", return_value=self.registry), \
             patch.object(server, "app_call", return_value=({"ok": True}, "request-1")) as call:
            result = server.tool_service_invoke({
                "service_id": "bumblebee-baseline", "action": "update",
                "expected_sha256": "a" * 64, "expected_commit": "b" * 40,
            })
        self.assertTrue(result["ok"])
        call.assert_called_once_with({
            "op": "service_update", "params": {
                "service_id": "bumblebee-baseline",
                "expected_sha256": "a" * 64,
                "expected_commit": "b" * 40,
            }}, 210)

    def test_update_refused_for_unregistered_service(self):
        with patch.object(server, "load_registry", return_value=self.registry), \
             patch.object(server, "app_call") as call:
            result = server.tool_service_invoke({"service_id": "factotum", "action": "update"})
        self.assertFalse(result["ok"])
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
