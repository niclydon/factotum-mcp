"""v1.10.0 verdict tests: KeepAlive-daemon job verdicts keyed on
the current instance, stored live-block decay, and the health
summary riders (outbox backlog, packages staleness).

The daemon case is Host2 ingest-contacts on 2026-10-19: a
KeepAlive daemon 26h into a healthy run read ok=false "last run
exited 256" because the probe keyed on the PREVIOUS instance's
retained exit record (a crash loop from the day before that had
self-recovered)."""
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import server

LABEL = "com.postgres.example-daemon"


def _status(last_exit=256, running=True, loaded=True):
    return {"loaded": loaded, "running": running,
            "pid": 777 if running else None,
            "last_exit_status": last_exit}


class KeepAliveDaemonVerdictTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = os.path.join(self.tmp.name, "daemon.log")
        self.heartbeat = os.path.join(self.tmp.name, "daemon.heartbeat")
        self.missing = os.path.join(self.tmp.name, "absent")

    def _health(self, log=None, heartbeat=None):
        return {"type": "job", "target": LABEL, "threshold_s": 5400,
                "log": self.missing if log is None else log,
                "heartbeat": self.missing if heartbeat is None
                else heartbeat,
                "success_pattern": "sync ok"}

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

    def test_running_daemon_previous_bad_exit_fresh_evidence_ok(self):
        self._touch(self.heartbeat, age_s=120)
        self._touch(self.log, age_s=120, body="[daemon] sync ok\n")
        ev = self._probe(_status(last_exit=256), 5, self._health(
            log=self.log, heartbeat=self.heartbeat))
        self.assertTrue(ev["ok"])
        self.assertEqual(ev["previous_exit_status"], 256)
        self.assertIn("history", ev["reason"])
        self.assertIn("current instance running", ev["reason"])

    def test_running_daemon_no_evidence_fails_as_running(self):
        ev = self._probe(_status(last_exit=256), 5, self._health())
        self.assertFalse(ev["ok"])
        self.assertIn("current instance running", ev["reason"])
        self.assertEqual(ev["previous_exit_status"], 256)

    def test_running_daemon_stale_evidence_still_fails(self):
        self._touch(self.heartbeat, age_s=999999)
        self._touch(self.log, age_s=999999, body="[daemon] sync ok\n")
        ev = self._probe(_status(last_exit=256), 5, self._health(
            log=self.log, heartbeat=self.heartbeat))
        self.assertFalse(ev["ok"])
        self.assertIn("stale", ev["reason"])

    def test_not_running_bad_exit_still_fails(self):
        ev = self._probe(_status(last_exit=256, running=False), 5,
                         self._health())
        self.assertFalse(ev["ok"])
        self.assertIn("exited 256", ev["reason"])
        self.assertNotIn("previous_exit_status", ev)

    def test_running_daemon_clean_exit_record_unaffected(self):
        self._touch(self.heartbeat, age_s=60)
        self._touch(self.log, age_s=60, body="[daemon] sync ok\n")
        ev = self._probe(_status(last_exit=0), 5, self._health(
            log=self.log, heartbeat=self.heartbeat))
        self.assertTrue(ev["ok"])
        self.assertTrue(ev["reason"].startswith("last exit 0"))
        self.assertNotIn("previous_exit_status", ev)


def _iso_ago(seconds):
    return (datetime.now(timezone.utc)
            - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class LiveBlockDecayTest(unittest.TestCase):
    def _block(self, age_s, ok=True, stale=None):
        b = {"loaded": True, "running": True, "pid": 1597,
             "probed_at": _iso_ago(age_s),
             "verdict": {"ok": ok, "reason": "HTTP 200",
                         "evidence_source": "http"}}
        if stale is not None:
            b["stale"] = stale
        return b

    def test_fresh_ok_block_untouched(self):
        b = self._block(60)
        out = server._decay_live_block(b)
        self.assertIs(out["verdict"]["ok"], True)
        self.assertNotIn("decayed", out)

    def test_stale_ok_block_decays(self):
        b = self._block(7200)
        out = server._decay_live_block(b)
        v = out["verdict"]
        self.assertIsNone(v["ok"])
        self.assertEqual(v["state"], "indeterminate")
        self.assertIn("stale attestation", v["reason"])
        self.assertIs(v["last_attested"]["ok"], True)
        self.assertEqual(v["last_attested"]["reason"], "HTTP 200")
        self.assertTrue(out["decayed"])
        # the stored block itself is not mutated
        self.assertIs(b["verdict"]["ok"], True)

    def test_stale_failure_block_untouched(self):
        b = self._block(7200, ok=False)
        out = server._decay_live_block(b)
        self.assertIs(out["verdict"]["ok"], False)
        self.assertNotIn("decayed", out)

    def test_shell_stale_mark_decays_even_when_fresh(self):
        b = self._block(60, stale=True)
        out = server._decay_live_block(b)
        self.assertIsNone(out["verdict"]["ok"])
        self.assertTrue(out["decayed"])

    def test_block_without_verdict_untouched(self):
        b = {"loaded": True, "probed_at": _iso_ago(7200)}
        self.assertEqual(server._decay_live_block(b), b)


class HealthSummaryRidersTest(unittest.TestCase):
    def _reg(self, outbox_depth, pkg_age_s):
        return {"host": "host1",
                "generated_at": _iso_ago(60),
                "services": [],
                "outbox_depth": outbox_depth,
                "packages": {
                    "measured_at": _iso_ago(pkg_age_s),
                    "counts": {}, "outdated": {}, "errors": []}}

    def test_backlog_and_stale_flags_set(self):
        with patch.object(server, "load_registry",
                          return_value=self._reg(501, 30 * 3600)):
            out = server.tool_health({})
        s = out["summary"]
        self.assertEqual(s["outbox_depth"], 501)
        self.assertIs(s["outbox_backlog"], True)
        self.assertIs(s["packages_stale"], True)
        self.assertGreater(s["packages_age_s"], 26 * 3600)

    def test_flags_clear_when_healthy(self):
        with patch.object(server, "load_registry",
                          return_value=self._reg(57, 3600)):
            out = server.tool_health({})
        s = out["summary"]
        self.assertEqual(s["outbox_depth"], 57)
        self.assertIs(s["outbox_backlog"], False)
        self.assertIs(s["packages_stale"], False)


if __name__ == "__main__":
    unittest.main()
