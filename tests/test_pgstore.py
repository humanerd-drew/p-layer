"""PostgreSQL backend tests. Skipped when no PG is reachable.

Set P_LAYER_TEST_PG_DSN to run (CI provides a postgres service container).
"""
import os
import tempfile
import unittest
from pathlib import Path

from p_layer.embed import HashEmbedder, NoopEmbedder
from p_layer.pgstore import PgStore

TEST_DSN = os.environ.get(
    "P_LAYER_TEST_PG_DSN", "dbname=p_layer_test host=/tmp port=55432 user=postgres"
)


def _pg_store(self, embedder=None):
    try:
        s = PgStore(TEST_DSN, embedder=embedder, vector_dim=768)
    except Exception as exc:  # no PG, missing deps, etc.
        self.skipTest(f"postgres unavailable: {exc}")
    self.addCleanup(s.close)
    s._execute(
        "TRUNCATE knowledge, episodes, entities, relations, rules, snapshots, audit_log "
        "RESTART IDENTITY CASCADE"
    )
    return s


class PgStoreTests(unittest.TestCase):
    def test_migrate_creates_schema(self):
        s = _pg_store(self)
        tables = {r["tablename"] for r in s._execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'", fetch=True)}
        for t in ("knowledge", "embeddings", "episodes", "entities", "relations", "rules",
                  "snapshots", "audit_log", "schema_migrations"):
            self.assertIn(t, tables)

    def test_write_recall_roundtrip(self):
        s = _pg_store(self, embedder=HashEmbedder(768))
        kid = s.add_knowledge("switched to portone v2", type="decision", layer="P5", who="system")
        self.assertIsInstance(kid, int)
        results = s.recall("portone", limit=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["type"], "decision")
        self.assertEqual(results[0]["layer"], "P5")

    def test_semantic_search_with_pgvector(self):
        s = _pg_store(self, embedder=HashEmbedder(768))
        s.add_knowledge("kubernetes billing migration", type="decision")
        s.add_knowledge("portone webhook retry policy", type="pattern")
        # hash vectors are pseudo-random: semantic may or may not rank the
        # expected entry first, but the path must run without crashing.
        results = s.semantic_search("billing kubernetes", limit=5)
        self.assertIsInstance(results, list)
        s2 = _pg_store(self, embedder=HashEmbedder(768))
        _ = s2

    def test_dimension_mismatch_warns_and_skips(self):
        s = _pg_store(self, embedder=HashEmbedder(64))  # 64-dim vs vector(768)
        s.add_knowledge("a fact", type="fact")
        self.assertIsNotNone(s.last_embed_warning)
        self.assertIn("dim", s.last_embed_warning)
        self.assertEqual(s.stats()["embeddings"], 0)

    def test_governance_enforced_and_audited(self):
        s = _pg_store(self)
        from p_layer.store import WriteDenied

        with self.assertRaises(WriteDenied):
            s.add_knowledge("secret", layer="P0", who="agent")
        denied = s.audit_log(denied_only=True)
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0]["action"], "write_denied")

    def test_supersede_snapshot_audit(self):
        s = _pg_store(self)
        kid = s.add_knowledge("use portone v2", type="decision")
        new = s.update_knowledge(kid, content="use portone v3")
        self.assertEqual(len(s.recall("portone")), 1)
        self.assertIn("v3", s.recall("portone")[0]["content"])
        s.snapshot_create("v1")
        s.add_knowledge("after snapshot", type="fact")
        n = s.snapshot_rollback("v1")
        self.assertEqual(n, 1)
        self.assertEqual(s.recall("after snapshot"), [])
        actions = [e["action"] for e in s.audit_log(limit=10)]
        for a in ("remember", "update", "snapshot_create", "snapshot_rollback"):
            self.assertIn(a, actions)

    def test_graph_rca(self):
        s = _pg_store(self)
        i = s.add_entity("deploy-failed", "incident")
        p = s.add_entity("retry-policy", "pattern")
        s.add_relation(i, p, "fixed_by")
        s.add_relation(p, i, "caused")
        result = s.graph_rca("deploy-failed")
        self.assertEqual(len(result["root_causes"]), 1)
        kinds = [(t["type"], t["entity"]) for t in result["root_causes"][0]["timeline"]]
        self.assertIn(("cause", "retry-policy"), kinds)
        self.assertIn(("fix", "retry-policy"), kinds)

    def test_ops_jobs_are_loud(self):
        s = _pg_store(self)
        for method in ("reembed", "consolidate", "compile_wiki"):
            with self.assertRaises(NotImplementedError):
                getattr(s, method)("/tmp/x")

    def test_stats_backend(self):
        s = _pg_store(self)
        self.assertEqual(s.stats()["backend"], "postgresql")
        self.assertIn("semantic_available", s.stats())

    def test_missing_dsn_raises(self):
        import p_layer.pgstore as pg

        old = pg.DEFAULT_DSN
        pg.DEFAULT_DSN = ""
        try:
            with self.assertRaises(ValueError):
                PgStore("")
        finally:
            pg.DEFAULT_DSN = old


class PgCliCompatTests(unittest.TestCase):
    def test_pg_layer_shim_points_nowhere(self):
        # The p-layers 1.0 compat layer is SQLite-only by design; PgStore is
        # the p_layer-native multi-user backend.
        self.assertEqual(PgStore.backend, "postgresql")


if __name__ == "__main__":
    unittest.main()
