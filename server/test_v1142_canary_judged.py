"""v1.14.2 canary judged-marker tests (internal ticket): a shell-emitted
'indeterminate' judgment (outcome + detail present, timestamp
missing or unparseable, the internal ticket shell 1.6.11 fold shape) is
authoritative in _overlay_canary_records: stored history must not
resurrect the older 'ok' it replaced. Fieldless and partial
incoming records keep the internal ticket preservation behavior."""
import unittest

import server


STORED_OK = {
    "last_exercised_at": "2026-10-01T04:34:05Z",
    "last_exercise_outcome": "ok",
    "last_exercise_detail": "ran fine",
}

JUDGED = {
    "id": "shortcuts.run",
    "last_exercise_outcome": "indeterminate",
    "last_exercise_detail":
        "canary attestation invalid: last_exercised_at missing or malformed",
}


def overlay(action, stored):
    reg = {"actions": [dict(action)]}
    source = {action["id"]: dict(stored)} if stored else {}
    out, changed = server._overlay_canary_records(reg, source)
    return out["actions"][0], changed


class JudgedMarkerTest(unittest.TestCase):
    def test_judged_marker_survives_older_stored_ok(self):
        # The internal ticket repro: registry marker + stored ok 2026-10-01.
        action, changed = overlay(JUDGED, STORED_OK)
        self.assertEqual(changed, 0)
        self.assertEqual(action["last_exercise_outcome"], "indeterminate")
        self.assertNotIn("last_exercised_at", action)

    def test_judged_marker_with_garbage_timestamp_survives(self):
        judged = dict(JUDGED, last_exercised_at="not-a-date")
        action, changed = overlay(judged, STORED_OK)
        self.assertEqual(changed, 0)
        self.assertEqual(action["last_exercise_outcome"], "indeterminate")
        self.assertEqual(action["last_exercised_at"], "not-a-date")

    def test_fieldless_action_still_preserved(self):
        # internal ticket: an older producer's fieldless action must not
        # erase stored history.
        action, changed = overlay({"id": "shortcuts.run"}, STORED_OK)
        self.assertEqual(changed, 1)
        self.assertEqual(action["last_exercise_outcome"], "ok")
        self.assertEqual(action["last_exercised_at"],
                         "2026-10-01T04:34:05Z")

    def test_partial_record_is_not_a_judgment(self):
        # Outcome text without detail text is fieldless input, not
        # a shell judgment: preservation still applies.
        partial = {"id": "shortcuts.run",
                   "last_exercise_outcome": "indeterminate"}
        action, changed = overlay(partial, STORED_OK)
        self.assertEqual(changed, 1)
        self.assertEqual(action["last_exercise_outcome"], "ok")

    def test_newer_stored_still_wins_over_valid_registry(self):
        current = {"id": "shortcuts.run",
                   "last_exercised_at": "2026-09-20T04:34:05Z",
                   "last_exercise_outcome": "ok",
                   "last_exercise_detail": "older run"}
        action, changed = overlay(current, STORED_OK)
        self.assertEqual(changed, 1)
        self.assertEqual(action["last_exercise_detail"], "ran fine")

    def test_newer_registry_still_stands_over_stored(self):
        current = {"id": "shortcuts.run",
                   "last_exercised_at": "2026-10-20T04:34:05Z",
                   "last_exercise_outcome": "ok",
                   "last_exercise_detail": "fresh run"}
        action, changed = overlay(current, STORED_OK)
        self.assertEqual(changed, 0)
        self.assertEqual(action["last_exercise_detail"], "fresh run")

    def test_judged_invalid_predicate(self):
        self.assertTrue(server._canary_judged_invalid(JUDGED))
        self.assertTrue(server._canary_judged_invalid(
            dict(JUDGED, last_exercised_at="garbage")))
        self.assertFalse(server._canary_judged_invalid(
            dict(JUDGED, last_exercised_at="2026-10-01T04:34:05Z")))
        self.assertFalse(server._canary_judged_invalid(
            {"last_exercise_outcome": "indeterminate"}))
        self.assertFalse(server._canary_judged_invalid({}))
        self.assertFalse(server._canary_judged_invalid(None))
        self.assertFalse(server._canary_judged_invalid(STORED_OK))


if __name__ == "__main__":
    unittest.main()
