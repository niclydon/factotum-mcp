import os
import tempfile
import time
import unittest
from unittest.mock import patch

import server

BOOT = "2026-10-18T23:59:36Z"
LABEL = "com.postgres.example-ingest"


def _status(last_exit=0, running=False, loaded=True):
    return {"loaded": loaded, "running": running,
            "pid": 4242 if running else None,
            "last_exit_status": last_exit}


class JobProbeNotYetRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = os.path.join(self.tmp.name, "job.log")
        self.heartbeat = os.path.join(self.tmp.name, "job.heartbeat")
        self.missing_log = os.path.join(self.tmp.name, "absent.log")
        self.missing_hb = os.path.join(self.tmp.name, "absent.heartbeat")

    def _health(self, log=None, heartbeat=None, pattern="Complete:"):
        return {"type": "job", "target": LABEL, "threshold_s": 25200,
                "log": self.missing_log if log is None else log,
                "heartbeat": self.missing_hb if heartbeat is None
                else heartbeat,
                "success_pattern": pattern}

    def _probe(self, status, runs, health):
        with patch.object(server, "launchd_status",
                          return_value=status), \
             patch.object(server, "_job_runs", return_value=runs):
            return server._job_probe(health)

    def _touch(self, path, age_s=60, body=""):
        with open(path, "w") as f:
            f.write(body)
        when = time.time() - age_s
        os.utime(path, (when, when))

    # --- the new state -------------------------------------------------

    def test_never_ran_no_evidence_is_not_yet_run(self):
        # The Host1-after-reboot case: launchctl list reports the
        # placeholder LastExitStatus 0, but print says runs = 0 and
        # neither evidence file exists.
        ev = self._probe(_status(last_exit=0), 0, self._health())
        self.assertIsNone(ev["ok"])
        self.assertEqual(ev["state"], "not_yet_run")
        # v1.10.0: the anchor is the agent's load (the runs
        # counter's scope), not the host boot.
        self.assertEqual(ev["anchor"], "agent_load")
        self.assertNotIn("boot_time", ev)
        self.assertIn("not yet run since agent load", ev["reason"])

    def test_never_ran_no_recorded_exit_is_not_yet_run(self):
        ev = self._probe(_status(last_exit=None), 0, self._health())
        self.assertIsNone(ev["ok"])
        self.assertEqual(ev["state"], "not_yet_run")

    def test_never_ran_heartbeat_only_log_missing_is_not_yet_run(self):
        # ingest-personalization's shape: heartbeat on durable
        # storage survives, the pattern-check log does not.
        self._touch(self.heartbeat, age_s=120)
        ev = self._probe(_status(last_exit=0), 0,
                         self._health(heartbeat=self.heartbeat))
        self.assertIsNone(ev["ok"])
        self.assertEqual(ev["state"], "not_yet_run")

    # --- the conservative boundary: existing logic unchanged ----------

    def test_ran_heartbeat_only_log_missing_still_fails(self):
        self._touch(self.heartbeat, age_s=120)
        ev = self._probe(_status(last_exit=0), 2,
                         self._health(heartbeat=self.heartbeat))
        self.assertFalse(ev["ok"])
        self.assertIn("success line in the log tail", ev["reason"])

    def test_ran_no_evidence_still_fails(self):
        ev = self._probe(_status(last_exit=0), 3, self._health())
        self.assertFalse(ev["ok"])
        self.assertEqual(ev["reason"],
                         "no heartbeat or log evidence found")

    def test_never_ran_with_fresh_success_evidence_still_ok(self):
        self._touch(self.log, age_s=60,
                    body="[job] Complete: 10 rows upserted\n")
        ev = self._probe(_status(last_exit=0), 0,
                         self._health(log=self.log))
        self.assertTrue(ev["ok"])

    def test_never_ran_with_stale_evidence_still_fails(self):
        self._touch(self.log, age_s=999999,
                    body="[job] Complete: 10 rows upserted\n")
        ev = self._probe(_status(last_exit=0), 0,
                         self._health(log=self.log))
        self.assertFalse(ev["ok"])
        self.assertIn("stale", ev["reason"])

    def test_never_ran_with_failed_exit_still_fails(self):
        ev = self._probe(_status(last_exit=256), 0, self._health())
        self.assertFalse(ev["ok"])
        self.assertIn("exited 256", ev["reason"])

    def test_runs_undeterminable_no_evidence_still_fails(self):
        # _job_runs returning None is 'no information': the gate
        # must not engage and the pre-1.9.0 verdict stands.
        ev = self._probe(_status(last_exit=0), None, self._health())
        self.assertFalse(ev["ok"])
        self.assertEqual(ev["reason"],
                         "no heartbeat or log evidence found")

    def test_not_loaded_unchanged(self):
        ev = self._probe(_status(loaded=False), 0, self._health())
        self.assertFalse(ev["ok"])
        self.assertIn("not loaded", ev["reason"])


if __name__ == "__main__":
    unittest.main()
