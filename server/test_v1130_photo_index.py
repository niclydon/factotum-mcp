"""v1.13.0 photo-index live query tests (internal ticket).

Fixture leo.sqlite + VUIndex.sqlite stores are built in a temp dir
with the real schemas' relevant columns; the tools are pointed at
them by patching the module's path constants. Assertions cover the
query semantics (the mirror's asset grain, exact lexeme membership
behind the instr() candidate scan, partner resolution, embedding
similarity ranking) and the read-only guarantee (fixture file bytes
unchanged after every tool ran).
"""
import hashlib
import os
import sqlite3
import struct
import tempfile
import unittest
import uuid as uuidlib
from unittest import mock

import server

ASSET_A = "AAAAAAAA-1111-4111-8111-111111111111"
ASSET_B = "BBBBBBBB-2222-4222-8222-222222222222"
ASSET_C = "CCCCCCCC-3333-4333-8333-333333333333"
ITEM_MOMENT = "DDDDDDDD-4444-4444-8444-444444444444"
BEANTOWN = 86269
CAT_BEANTOWN = 4120


def _pack(ids):
    return struct.pack("<%di" % len(ids), *ids)


def _emb(*head):
    vec = [0.0] * 128
    for i, v in enumerate(head):
        vec[i] = v
    return struct.pack("<128f", *vec)


def build_leo(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE items (pk INTEGER, identifier TEXT, type INTEGER,"
        " lexeme_ids BLOB, date_created REAL, change_timestamp REAL,"
        " lexeme_scores BLOB, thumbnail_info BLOB);"
        "CREATE TABLE lexicon (pk INTEGER, lexeme_id INTEGER,"
        " type INTEGER, category INTEGER, content TEXT,"
        " identifier TEXT, score REAL);")
    conn.executemany(
        "INSERT INTO lexicon VALUES (?,?,?,?,?,?,?)",
        [(1, BEANTOWN, 1, CAT_BEANTOWN, "beantown", "", None),
         (2, 5, 1, 4000, "boston", "", None),
         (3, 7, 2, 2170, "America", "", None),
         (4, 9, 1, 8050, "Beantown photo.jpg", "", None),
         (5, 11, 1, 4000, "", "", None),
         (6, 5, 2, 4000, "Boston town", "", None)])
    conn.executemany(
        "INSERT INTO items VALUES (?,?,?,?,?,?,?,?)",
        [(1, ASSET_A, 1, _pack([BEANTOWN, 5, 11, BEANTOWN]), 0, 0,
          None, None),
         (2, ASSET_B, 6, _pack([BEANTOWN]), 0, 0, None, None),
         (3, ITEM_MOMENT, 2, _pack([BEANTOWN]), 0, 0, None, None),
         (4, ASSET_C, 1, _pack([7]), 0, 0, None, None)])
    conn.commit()
    conn.close()


def build_vu(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE ZVUINDEXOBSERVATION (Z_PK INTEGER, ZIDENTIFIER"
        " INTEGER, ZTYPE INTEGER, ZSOURCE INTEGER, ZCLIENT INTEGER,"
        " ZISPRIMARY INTEGER, ZCONFIDENCE FLOAT, ZQUALITY FLOAT,"
        " ZASSET BLOB, ZMOMENT BLOB, ZEMBEDDING BLOB,"
        " ZCONTEXTUALEMBEDDING BLOB, ZMAPPING INTEGER);"
        "CREATE TABLE ZVUINDEXMAPPING (Z_PK INTEGER, ZLABEL INTEGER,"
        " ZPARTITION INTEGER, ZPARTNER INTEGER, ZOBSERVATION INTEGER,"
        " ZSIMILARITY FLOAT, ZDENSITY FLOAT, ZDIVERSITY FLOAT,"
        " ZCLUSTERINGSTATUS INTEGER);")
    a, b, c = (uuidlib.UUID(x).bytes for x in (ASSET_A, ASSET_B, ASSET_C))
    moment = uuidlib.UUID(ITEM_MOMENT).bytes
    conn.executemany(
        "INSERT INTO ZVUINDEXOBSERVATION VALUES (?,?,?,?,?,?,?,?,?,?,"
        "?,?,?)",
        [(1, 9001, 2, 1, 0, 1, 0.99, 1.0, a, moment, _emb(1.0, 0.0),
          None, 101),
         (2, 9002, 2, 1, 0, 1, 0.98, 1.0, b, moment,
          _emb(0.995, 0.0998), None, 102),
         (3, 9003, 2, 1, 0, 1, 0.97, 1.0, c, moment, _emb(0.0, 1.0),
          None, 103),
         (4, 9004, 6, 2, -1, 0, 0.50, 1.0, a, moment, None,
          _emb(0.0, 0.0, 1.0), 104)])
    conn.executemany(
        "INSERT INTO ZVUINDEXMAPPING VALUES (?,?,?,?,?,?,?,?,?)",
        [(101, 424242, -1, 9002, 1, 0.91, -1.0, 0.0, 2),
         (102, 424242, -1, 9001, 2, 0.91, -1.0, 0.0, 2),
         (103, 777, -1, -1, 3, 0.0, -1.0, 0.0, 2),
         (104, 555, 12, 9001, 4, 0.42, 0.5, 0.25, 3)])
    conn.commit()
    conn.close()


class PhotoIndexFixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="photo-index-test-")
        self.leo = os.path.join(self.dir, "leo.sqlite")
        self.vu = os.path.join(self.dir, "VUIndex.sqlite")
        build_leo(self.leo)
        build_vu(self.vu)
        server._VU_POOL_CACHE.update(
            {"key": None, "pool": None, "skipped": 0})
        self._patches = [
            mock.patch.dict(server.LEO_LIBRARY_PATHS,
                            {"main": self.leo, "syndication": self.leo}),
            mock.patch.object(server, "VU_INDEX_PATH", self.vu),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _hash(self, path):
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()


class LeoAssetTermsTest(PhotoIndexFixture):
    def test_terms_in_index_order_deduped(self):
        out = server.tool_leo_asset_terms({"asset": ASSET_A})
        self.assertTrue(out["found"])
        self.assertEqual(out["item_type"], 1)
        # lexeme 5 has two surface forms (canonical + variant);
        # both are terms, canonical first.
        self.assertEqual([t["content"] for t in out["terms"]],
                         ["beantown", "boston", "Boston town"])
        self.assertEqual([t["canonical"] for t in out["terms"]],
                         [True, True, False])
        self.assertEqual(out["terms"][0]["category"], CAT_BEANTOWN)
        # lexeme 11 is attached but content-empty (structural):
        # counted as a lexeme, not returned as a term.
        self.assertEqual(out["lexeme_count"], 3)
        self.assertEqual(out["term_count"], 3)

    def test_unknown_asset_reports_not_found(self):
        out = server.tool_leo_asset_terms(
            {"asset": "EEEEEEEE-5555-4555-8555-555555555555"})
        self.assertFalse(out["found"])

    def test_missing_asset_arg_is_an_error(self):
        self.assertIn("error", server.tool_leo_asset_terms({}))


class LeoTermAssetsTest(PhotoIndexFixture):
    def test_beantown_exact_returns_asset_grain_only(self):
        out = server.tool_leo_term_assets({"term": "beantown"})
        self.assertEqual(out["total_assets"], 2)
        self.assertEqual({a["asset"] for a in out["assets"]},
                         {ASSET_A, ASSET_B})
        self.assertEqual(out["other_items"], 1)  # the type-2 moment
        self.assertEqual(out["lexemes"][0]["lexeme_id"], BEANTOWN)
        self.assertEqual(out["lexemes"][0]["asset_count"], 2)

    def test_category_filter(self):
        out = server.tool_leo_term_assets(
            {"term": "beantown", "category": 9999})
        self.assertEqual(out["total_assets"], 0)
        self.assertEqual(out["lexemes"], [])

    def test_contains_match_sees_filename_lexeme(self):
        out = server.tool_leo_term_assets(
            {"term": "beantown", "match": "contains"})
        contents = {lex["content"] for lex in out["lexemes"]}
        self.assertEqual(contents, {"beantown", "Beantown photo.jpg"})

    def test_bad_match_is_an_error(self):
        out = server.tool_leo_term_assets({"term": "x", "match": "fuzzy"})
        self.assertIn("error", out)


class LeoStatsTest(PhotoIndexFixture):
    def test_counts(self):
        out = server.tool_leo_stats({"library": "main"})
        block = out["libraries"][0]
        self.assertEqual(block["lexicon_rows"], 6)
        self.assertEqual(block["content_lexemes"], 5)
        self.assertEqual(block["items"], 4)
        self.assertEqual(block["asset_items"], 3)


class VuObservationsTest(PhotoIndexFixture):
    def test_observations_with_mapping_and_partner_asset(self):
        out = server.tool_vu_asset_observations({"asset": ASSET_A})
        self.assertTrue(out["found"])
        self.assertEqual(out["observation_count"], 2)
        primary = out["observations"][0]
        self.assertTrue(primary["is_primary"])
        self.assertTrue(primary["has_embedding"])
        self.assertEqual(primary["mapping"]["label"], 424242)
        self.assertEqual(primary["mapping"]["partner_asset"], ASSET_B)
        self.assertEqual(primary["moment"], ITEM_MOMENT)

    def test_bad_uuid_is_an_error(self):
        out = server.tool_vu_asset_observations({"asset": "not-a-uuid"})
        self.assertIn("error", out)


class VuLabelAssetsTest(PhotoIndexFixture):
    def test_label_assets(self):
        out = server.tool_vu_label_assets({"label": 424242})
        self.assertEqual(out["total_assets"], 2)
        self.assertEqual({a["asset"] for a in out["assets"]},
                         {ASSET_A, ASSET_B})


class VuSimilarAssetsTest(PhotoIndexFixture):
    def test_nearest_neighbour_ranks_first_and_excludes_self(self):
        out = server.tool_vu_similar_assets({"asset": ASSET_A})
        self.assertTrue(out["found"])
        self.assertEqual(out["pool_assets"], 3)
        self.assertEqual(out["similar"][0]["asset"], ASSET_B)
        self.assertGreater(out["similar"][0]["similarity"], 0.99)
        self.assertNotIn(ASSET_A, [s["asset"] for s in out["similar"]])
        sims = {s["asset"]: s["similarity"] for s in out["similar"]}
        self.assertAlmostEqual(sims[ASSET_C], 0.0, places=5)

    def test_second_call_uses_cached_pool_same_answer(self):
        first = server.tool_vu_similar_assets({"asset": ASSET_A})
        self.assertIsNotNone(server._VU_POOL_CACHE["pool"])
        second = server.tool_vu_similar_assets({"asset": ASSET_A})
        self.assertEqual(first["similar"], second["similar"])
        self.assertEqual(first["pool_assets"], second["pool_assets"])


class ReadOnlyTest(PhotoIndexFixture):
    def test_stores_byte_identical_after_all_tools(self):
        before = (self._hash(self.leo), self._hash(self.vu))
        server.tool_leo_asset_terms({"asset": ASSET_A})
        server.tool_leo_term_assets({"term": "beantown"})
        server.tool_leo_stats({"library": "all"})
        server.tool_vu_asset_observations({"asset": ASSET_A})
        server.tool_vu_label_assets({"label": 424242})
        server.tool_vu_similar_assets({"asset": ASSET_A})
        self.assertEqual(before, (self._hash(self.leo),
                                  self._hash(self.vu)))
        self.assertEqual(
            [f for f in os.listdir(self.dir)
             if f.endswith(("-wal", "-shm", "-journal"))], [])


class RegistrationTest(unittest.TestCase):
    def test_new_tools_listed_and_dispatched(self):
        names = {t["name"] for t in server.TOOLS}
        for name in ("factotum_leo_asset_terms", "factotum_leo_term_assets",
                     "factotum_leo_stats", "factotum_vu_asset_observations",
                     "factotum_vu_label_assets", "factotum_vu_similar_assets"):
            self.assertIn(name, names)
            self.assertIn(name, server.DISPATCH)


if __name__ == "__main__":
    unittest.main()
