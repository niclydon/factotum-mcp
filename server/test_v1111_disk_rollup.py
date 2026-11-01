"""v1.11.1 disk_rollup tests: the boot-anchored headline rule
mirroring the shell's computeDiskState since shell 1.6.4
(scan #3 item 5).

The Host1 shape is the live one that motivated the change:
boot volume at ~20% free, Time Machine destination at ~3%
free by design. Under the pre-1.11.1 worst-volume rule
disk_rollup read "critical" permanently; boot-anchored it
reads "ok", matching the registry's own disk.state."""
import unittest

import server


def _reg(volumes, watched=None, caps=None):
    return {"disk": {
        "measured_at": "2026-10-19T12:00:00Z",
        "thresholds": {"warn_free_pct": 15,
                       "critical_free_pct": 8,
                       "watched_caps_bytes": caps or {}},
        "volumes": volumes,
        "watched": watched or []}}


BOOT_OK = {"mount": "/", "total_bytes": 500_000_000_000,
           "free_bytes": 100_000_000_000, "boot": True}      # 20%
TM_FULL = {"mount": "/Volumes/TimeMachine",
           "total_bytes": 2_000_000_000_000,
           "free_bytes": 60_000_000_000, "boot": False}      # 3%


class BootAnchoredTest(unittest.TestCase):
    def test_host1_shape_reads_ok(self):
        out = server.disk_rollup(_reg([BOOT_OK, TM_FULL]))
        self.assertEqual(out["state"], "ok")
        self.assertEqual(out["reasons"], [])
        # Non-boot volumes remain in the per-volume detail.
        self.assertEqual(len(out["volumes"]), 2)
        self.assertEqual(out["volumes"][1]["free_pct"], 3.0)

    def test_boot_critical_drives_critical(self):
        boot = dict(BOOT_OK, free_bytes=25_000_000_000)     # 5%
        other = {"mount": "/Volumes/Data",
                 "total_bytes": 1_000_000_000_000,
                 "free_bytes": 500_000_000_000, "boot": False}
        out = server.disk_rollup(_reg([boot, other]))
        self.assertEqual(out["state"], "critical")
        self.assertEqual(len(out["reasons"]), 1)
        self.assertTrue(out["reasons"][0].startswith("/ free"))

    def test_boot_warn_drives_warn(self):
        boot = dict(BOOT_OK, free_bytes=60_000_000_000)     # 12%
        out = server.disk_rollup(_reg([boot, TM_FULL]))
        self.assertEqual(out["state"], "warn")

    def test_numeric_boot_flag_counts(self):
        boot = dict(BOOT_OK, boot=1)
        out = server.disk_rollup(_reg([boot, TM_FULL]))
        self.assertEqual(out["state"], "ok")

    def test_watched_over_cap_still_critical(self):
        watched = [{"id": "staging", "bytes": 5000}]
        out = server.disk_rollup(
            _reg([BOOT_OK, TM_FULL], watched=watched,
                 caps={"staging": 4000}))
        self.assertEqual(out["state"], "critical")
        self.assertIn("watched path staging", out["reasons"][0])


class WorstVolumeFallbackTest(unittest.TestCase):
    """No boot flag anywhere (registries predating the flag):
    the pre-1.11.1 worst-volume rule applies unchanged."""

    def test_unflagged_full_volume_is_critical(self):
        vols = [{"mount": "/", "total_bytes": 500_000_000_000,
                 "free_bytes": 100_000_000_000},
                {"mount": "/Volumes/TimeMachine",
                 "total_bytes": 2_000_000_000_000,
                 "free_bytes": 60_000_000_000}]
        out = server.disk_rollup(_reg(vols))
        self.assertEqual(out["state"], "critical")

    def test_unflagged_warn_volume_warns(self):
        vols = [{"mount": "/", "total_bytes": 500_000_000_000,
                 "free_bytes": 60_000_000_000}]             # 12%
        out = server.disk_rollup(_reg(vols))
        self.assertEqual(out["state"], "warn")

    def test_boot_false_everywhere_is_unflagged(self):
        vols = [dict(BOOT_OK, boot=False), TM_FULL]
        out = server.disk_rollup(_reg(vols))
        self.assertEqual(out["state"], "critical")


class ShapeTest(unittest.TestCase):
    def test_no_disk_section_returns_none(self):
        self.assertIsNone(server.disk_rollup({}))
        self.assertIsNone(server.disk_rollup(None))

    def test_boot_volume_without_total_is_skipped(self):
        boot = {"mount": "/", "total_bytes": 0, "free_bytes": 0,
                "boot": True}
        out = server.disk_rollup(_reg([boot, TM_FULL]))
        self.assertEqual(out["state"], "ok")
        self.assertIsNone(out["volumes"][0]["free_pct"])


if __name__ == "__main__":
    unittest.main()
