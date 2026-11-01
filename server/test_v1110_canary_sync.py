"""v1.11.0 canary-sync tests: the factotum_actions rows the sync
builds from registry actions, the column probe, and the
unchanged-registry backfill in sync_push (internal ticket, Postgres
migration 2365).

The exercised values are the Oct 19, 2026 supervised canary
passes: Host2 shortcuts.run at 2026-10-19T04:34:05Z and Host1
eventkit.reminder_create at 2026-10-19T04:36:24Z. The row
builder is pure; the probe runs against a fake cursor; the
sync_push tests run the real function against a fake psycopg
connection so the content-hash short-circuit path is covered
(the adversarial-review blocker: an unchanged registry must
still backfill the canary columns)."""
import sys
import types
import unittest
from datetime import datetime, timezone

import server


EXERCISED = {
    "id": "shortcuts.run",
    "app_bundle_id": "io.example.shortcutsbridge",
    "name": "Run shortcut",
    "op": "run",
    "mechanism": "shortcuts-cli",
    "risk": "medium",
    "status": "native",
    "last_exercised_at": "2026-10-19T04:34:05Z",
    "last_exercise_outcome": "ok",
    "last_exercise_detail": "ran 'Copy Across Devices' via "
                            "shortcuts-mcp (returncode 0)",
}

UNEXERCISED = {
    "id": "eventkit.reminder_create",
    "app_bundle_id": "io.example.eventkitbridge",
    "name": "Create reminder",
    "op": "create",
    "mechanism": "eventkit",
    "risk": "low",
    "status": "native",
}

EXERCISED_DT = datetime(2026, 10, 19, 4, 34, 5,
                        tzinfo=timezone.utc)


class ActionRowsTest(unittest.TestCase):
    def test_canary_row_carries_exercise_fields(self):
        rows = server._action_rows("host2", [EXERCISED], True)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(len(row), 11)
        self.assertEqual(row[:8], ("host2", "shortcuts.run",
                                   "io.example.shortcutsbridge",
                                   "Run shortcut", "run",
                                   "shortcuts-cli", "medium",
                                   "native"))
        self.assertEqual(row[8], EXERCISED_DT)
        self.assertEqual(row[9], "ok")
        self.assertEqual(row[10], EXERCISED["last_exercise_detail"])

    def test_canary_row_unexercised_is_nulls(self):
        row = server._action_rows("host1", [UNEXERCISED], True)[0]
        self.assertEqual(len(row), 11)
        self.assertEqual(row[8:], (None, None, None))

    def test_legacy_row_shape_unchanged(self):
        row = server._action_rows("host2", [EXERCISED], False)[0]
        self.assertEqual(len(row), 8)
        self.assertEqual(row[1], "shortcuts.run")

    def test_actions_without_id_are_skipped(self):
        rows = server._action_rows("host2", [{"name": "no id"},
                                             EXERCISED], True)
        self.assertEqual(len(rows), 1)

    def test_unparseable_timestamp_degrades_to_none(self):
        bad = dict(EXERCISED, last_exercised_at="not-a-time")
        row = server._action_rows("host2", [bad], True)[0]
        self.assertIsNone(row[8])
        self.assertEqual(row[9], "ok")

    def test_unparseable_timestamp_is_reported(self):
        bad = dict(EXERCISED, last_exercised_at="not-a-time")
        rows, bad_ts = server._action_rows_detailed(
            "host2", [bad, UNEXERCISED], True)
        self.assertEqual(bad_ts, ["shortcuts.run"])
        self.assertIsNone(rows[0][8])

    def test_epoch_integer_timestamp_is_parsed(self):
        epoch = int(EXERCISED_DT.timestamp())
        row = server._action_rows(
            "host2", [dict(EXERCISED, last_exercised_at=epoch)],
            True)[0]
        self.assertEqual(row[8], EXERCISED_DT)

    def test_epoch_float_timestamp_is_parsed(self):
        epoch = EXERCISED_DT.timestamp()
        row = server._action_rows(
            "host2", [dict(EXERCISED, last_exercised_at=epoch)],
            True)[0]
        self.assertEqual(row[8], EXERCISED_DT)

    def test_control_characters_are_stripped_from_detail(self):
        poisoned = dict(EXERCISED,
                        last_exercise_detail="a\x00b\x07c\nd")
        row = server._action_rows("host2", [poisoned], True)[0]
        self.assertEqual(row[10], "abc\nd")

    def test_non_string_outcome_and_detail_drop_to_none(self):
        weird = dict(EXERCISED, last_exercise_outcome={"x": 1},
                     last_exercise_detail=42)
        row = server._action_rows("host2", [weird], True)[0]
        self.assertEqual(row[9:], (None, None))

    def test_detail_is_capped(self):
        big = dict(EXERCISED, last_exercise_detail="x" * 5000)
        row = server._action_rows("host2", [big], True)[0]
        self.assertEqual(len(row[10]), server._CANARY_DETAIL_LIMIT)


CANARY_COLS = [("last_exercised_at", "timestamp with time zone"),
               ("last_exercise_outcome", "text"),
               ("last_exercise_detail", "text")]


class _FakeProbeCursor:
    def __init__(self, col_rows=None, raises=False):
        self._col_rows = col_rows
        self._raises = raises
        self.queries = []

    def execute(self, sql, params=None):
        self.queries.append(sql)
        if self._raises:
            raise RuntimeError("catalog unavailable")

    def fetchall(self):
        return list(self._col_rows or [])


class CanaryProbeTest(unittest.TestCase):
    def test_all_three_columns_present(self):
        cur = _FakeProbeCursor(col_rows=CANARY_COLS)
        self.assertTrue(server._actions_canary_columns(cur))
        self.assertIn("table_schema = 'public'", cur.queries[0])

    def test_columns_absent(self):
        self.assertFalse(server._actions_canary_columns(
            _FakeProbeCursor(col_rows=[])))

    def test_partial_columns_count_as_absent(self):
        self.assertFalse(server._actions_canary_columns(
            _FakeProbeCursor(col_rows=CANARY_COLS[:2])))

    def test_wrong_column_type_counts_as_absent(self):
        wrong = [("last_exercised_at", "text"),
                 ("last_exercise_outcome", "text"),
                 ("last_exercise_detail", "text")]
        self.assertFalse(server._actions_canary_columns(
            _FakeProbeCursor(col_rows=wrong)))

    def test_probe_failure_degrades_false(self):
        self.assertFalse(server._actions_canary_columns(
            _FakeProbeCursor(raises=True)))

    def test_negative_is_not_cached(self):
        # A server that probed before the migration landed must
        # pick the columns up on a later sync without a restart.
        self.assertFalse(server._actions_canary_columns(
            _FakeProbeCursor(col_rows=[])))
        self.assertTrue(server._actions_canary_columns(
            _FakeProbeCursor(col_rows=CANARY_COLS)))

    def test_positive_is_not_cached(self):
        # If the columns are dropped again (the migration's
        # documented rollback) a running server must degrade to
        # the legacy column set, not keep issuing 11-column
        # inserts that fail every sync until a restart.
        self.assertTrue(server._actions_canary_columns(
            _FakeProbeCursor(col_rows=CANARY_COLS)))
        self.assertFalse(server._actions_canary_columns(
            _FakeProbeCursor(col_rows=[])))


class _FakeSyncCursor:
    """Dispatches on SQL shape: registries hash lookup, canary
    probe, stored action rows; records everything else."""

    def __init__(self, chash, col_rows, stored_actions, registry=None,
                 fail_history=False):
        self._chash = chash
        self._col_rows = col_rows
        self._stored = stored_actions
        self._registry = registry
        self._fail_history = fail_history
        self._last = ""
        self.executed = []
        self.executemany_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._last = sql
        self.executed.append((sql, params))
        if self._fail_history and sql.startswith("SELECT action_id, last_exercised_at"):
            raise RuntimeError("history unavailable")

    def executemany(self, sql, rows):
        self.executemany_calls.append((sql, list(rows)))

    def fetchone(self):
        if "FROM factotum_registries" in self._last:
            return (self._chash, self._registry)
        return None

    def fetchall(self):
        if "information_schema" in self._last:
            return list(self._col_rows)
        if "FROM public.factotum_actions" in self._last:
            return list(self._stored)
        return []


class _FakeSyncConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1


class SyncPushBackfillTest(unittest.TestCase):
    """The unchanged-registry path: sync_push must backfill the
    canary columns from the blob even when the content hash
    matches (the deploy-time backfill the change promises)."""

    REG = {"generated_at": "2026-10-19T04:40:00Z",
           "actions": [EXERCISED, UNEXERCISED],
           "apps": [], "services": []}

    def setUp(self):
        self._saved = {}
        for name in ("psycopg", "psycopg.types",
                     "psycopg.types.json"):
            self._saved[name] = sys.modules.get(name)
        psycopg = types.ModuleType("psycopg")
        ptypes = types.ModuleType("psycopg.types")
        pjson = types.ModuleType("psycopg.types.json")
        pjson.Jsonb = lambda v: v
        ptypes.json = pjson
        psycopg.types = ptypes
        sys.modules["psycopg"] = psycopg
        sys.modules["psycopg.types"] = ptypes
        sys.modules["psycopg.types.json"] = pjson
        self._attrs = {}
        for name in ("_config_flag", "load_registry",
                     "assembled_registry", "_pg_connect",
                     "_record_sync", "HOST_LABEL"):
            self._attrs[name] = getattr(server, name)
        server._config_flag = lambda *a, **k: True
        server.load_registry = lambda: dict(self.REG)
        server.assembled_registry = lambda reg, **kwargs: reg
        server._record_sync = lambda *a, **k: None
        server.HOST_LABEL = "Host2"

    def tearDown(self):
        for name, mod in self._saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        for name, val in self._attrs.items():
            setattr(server, name, val)

    def _run(self, col_rows, stored, registry=None):
        chash = server.canonical_hash(dict(self.REG))
        cur = _FakeSyncCursor(chash, col_rows, stored, registry)
        conn = _FakeSyncConn(cur)
        server._pg_connect = lambda: conn
        out = server.sync_push("test")
        return out, cur, conn

    def test_unchanged_registry_backfills_differing_rows(self):
        stored = [("shortcuts.run", None, None, None),
                  ("eventkit.reminder_create", None, None, None)]
        out, cur, conn = self._run(CANARY_COLS, stored)
        self.assertTrue(out["ok"])
        self.assertFalse(out["changed"])
        self.assertTrue(out["canary_columns"])
        self.assertEqual(out["canary_backfilled"], 1)
        updates = [(sql, p) for sql, p in cur.executed
                   if sql.startswith("UPDATE public.factotum_actions")]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][1],
                         (EXERCISED_DT, "ok",
                          EXERCISED["last_exercise_detail"],
                          "host2", "shortcuts.run"))
        self.assertEqual(conn.commits, 1)

    def test_unchanged_registry_matching_rows_no_updates(self):
        stored = [("shortcuts.run", EXERCISED_DT, "ok",
                   EXERCISED["last_exercise_detail"]),
                  ("eventkit.reminder_create", None, None, None)]
        out, cur, conn = self._run(CANARY_COLS, stored)
        self.assertTrue(out["ok"])
        self.assertNotIn("canary_backfilled", out)
        updates = [sql for sql, p in cur.executed
                   if sql.startswith("UPDATE public.factotum_actions")]
        self.assertEqual(updates, [])

    def test_unchanged_registry_without_columns_skips_backfill(self):
        out, cur, conn = self._run([], [])
        self.assertTrue(out["ok"])
        self.assertFalse(out["canary_columns"])
        updates = [sql for sql, p in cur.executed
                   if sql.startswith("UPDATE public.factotum_actions")
                   or "FROM public.factotum_actions" in sql]
        self.assertEqual(updates, [])

    def test_backfill_reports_unparseable_timestamp(self):
        reg = dict(self.REG)
        reg["actions"] = [dict(EXERCISED,
                               last_exercised_at="not-a-time"),
                          UNEXERCISED]
        server.load_registry = lambda: dict(reg)
        chash = server.canonical_hash(reg)
        stored = [("shortcuts.run", None, "ok",
                   EXERCISED["last_exercise_detail"]),
                  ("eventkit.reminder_create", None, None, None)]
        cur = _FakeSyncCursor(chash, CANARY_COLS, stored)
        conn = _FakeSyncConn(cur)
        server._pg_connect = lambda: conn
        out = server.sync_push("test")
        self.assertTrue(out["ok"])
        self.assertIn("shortcuts.run", out["canary_note"])

    def test_fieldless_registry_preserves_postgres_history_in_blob_and_rows(self):
        fieldless = dict(self.REG)
        fieldless["actions"] = [{k: v for k, v in EXERCISED.items()
                                 if not k.startswith("last_exercise") and
                                 k != "last_exercised_at"}, UNEXERCISED]
        server.load_registry = lambda: fieldless
        cur = _FakeSyncCursor(server.canonical_hash(fieldless), CANARY_COLS,
                              [("shortcuts.run", EXERCISED_DT, "ok",
                                EXERCISED["last_exercise_detail"])], fieldless)
        conn = _FakeSyncConn(cur)
        server._pg_connect = lambda: conn
        out = server.sync_push("test")
        self.assertTrue(out["ok"])
        self.assertTrue(out["changed"])
        self.assertIn("preserved 1 newer Postgres canary", out["canary_note"])
        blob = next(params[3] for sql, params in cur.executed
                    if sql.startswith("INSERT INTO factotum_registries"))
        self.assertEqual(blob["actions"][0]["last_exercised_at"],
                         EXERCISED["last_exercised_at"])
        actions = next(rows for sql, rows in cur.executemany_calls
                       if "INSERT INTO public.factotum_actions" in sql)
        self.assertEqual(actions[0][8:],
                         (EXERCISED_DT, "ok", EXERCISED["last_exercise_detail"]))
        self.assertEqual(conn.commits, 1)

    def test_history_probe_failure_rolls_back_and_retry_preserves(self):
        fieldless = dict(self.REG)
        fieldless["actions"] = [UNEXERCISED]
        server.load_registry = lambda: fieldless
        stored = [("eventkit.reminder_create", EXERCISED_DT, "ok", "private detail")]
        failed_cur = _FakeSyncCursor(server.canonical_hash(fieldless),
                                      CANARY_COLS, stored, fieldless,
                                      fail_history=True)
        failed_conn = _FakeSyncConn(failed_cur)
        server._pg_connect = lambda: failed_conn
        failed = server.sync_push("test")
        self.assertFalse(failed["ok"])
        self.assertEqual(failed_conn.commits, 0)
        self.assertFalse(any(sql.startswith("DELETE FROM factotum_actions")
                             for sql, _ in failed_cur.executed))
        retry_cur = _FakeSyncCursor(server.canonical_hash(fieldless),
                                    CANARY_COLS, stored, fieldless)
        retry_conn = _FakeSyncConn(retry_cur)
        server._pg_connect = lambda: retry_conn
        retried = server.sync_push("test")
        self.assertTrue(retried["ok"])
        self.assertEqual(retry_conn.commits, 1)
        self.assertTrue(all(params == ("host2",) for sql, params in retry_cur.executed
                            if "FROM public.factotum_actions WHERE host" in sql))

    def test_state_file_is_applied_on_real_sync_push_path(self):
        import json
        import tempfile
        from pathlib import Path
        fieldless = dict(self.REG)
        fieldless["actions"] = [dict(UNEXERCISED, id="shortcuts.run")]
        server.load_registry = lambda: fieldless
        server.assembled_registry = self._attrs["assembled_registry"]
        old_support = server.SUPPORT
        try:
            with tempfile.TemporaryDirectory() as tmp:
                server.SUPPORT = tmp
                Path(tmp, "canary-state.json").write_text(json.dumps({
                    "updated_at": "2026-10-19T04:34:05Z",
                    "actions": {"shortcuts.run": EXERCISED}}))
                cur = _FakeSyncCursor("old", CANARY_COLS, [])
                server._pg_connect = lambda: _FakeSyncConn(cur)
                out = server.sync_push("test")
                self.assertTrue(out["ok"])
                blob = next(params[3] for sql, params in cur.executed
                            if sql.startswith("INSERT INTO factotum_registries"))
                self.assertEqual(blob["actions"][0]["last_exercise_outcome"], "ok")
        finally:
            server.SUPPORT = old_support


class CanaryHistoryMergeTest(unittest.TestCase):
    def test_newer_registry_wins_over_stale_state(self):
        current = {"actions": [EXERCISED]}
        older = dict(EXERCISED, last_exercised_at="2026-10-18T04:34:05Z",
                     last_exercise_outcome="fail")
        merged, count = server._overlay_canary_records(
            current, {"shortcuts.run": older})
        self.assertEqual(count, 0)
        self.assertEqual(merged["actions"][0], EXERCISED)

    def test_removed_action_is_not_resurrected(self):
        current = {"actions": [UNEXERCISED]}
        merged, count = server._overlay_canary_records(
            current, {"shortcuts.run": EXERCISED})
        self.assertEqual(count, 0)
        self.assertEqual(merged, current)

    def test_malformed_state_is_ignored_without_detail_in_note(self):
        import json
        import tempfile
        from pathlib import Path
        old_support = server.SUPPORT
        try:
            with tempfile.TemporaryDirectory() as tmp:
                server.SUPPORT = tmp
                Path(tmp, "canary-state.json").write_text(json.dumps({
                    "actions": {"shortcuts.run": EXERCISED,
                                "unknown.action": EXERCISED,
                                "broken": {"last_exercised_at": "not-a-time",
                                           "last_exercise_detail": "PRIVATE"}}}))
                state, malformed = server._load_canary_state()
                self.assertEqual(malformed, 1)
                reg, count = server._overlay_canary_records(
                    {"actions": [dict(UNEXERCISED, id="shortcuts.run")]}, state)
                self.assertEqual(count, 1)
                self.assertEqual(len(reg["actions"]), 1)
                self.assertEqual(reg["actions"][0]["last_exercise_outcome"], "ok")
        finally:
            server.SUPPORT = old_support


if __name__ == "__main__":
    unittest.main()
