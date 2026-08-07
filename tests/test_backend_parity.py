"""Backend parity — the same behavioral assertions run against both the
SQLite `Store` and the PostgreSQL `PgStore`. This is the guard against the
dual-backend drift that plagued p-layer: if the two engines ever disagree,
these tests fail.

PG side skips when no PostgreSQL is reachable (P_LAYER_TEST_PG_DSN).
"""
import os
import tempfile
import unittest
from pathlib import Path

from p_layer.embed import NoopEmbedder
from p_layer.store import Store, WriteDenied

TEST_DSN = os.environ.get(
    "P_LAYER_TEST_PG_DSN", "dbname=p_layer_test host=/tmp port=55432 user=postgres"
)


class BackendParityBase:  # mixin — not a TestCase, so unittest never collects it
    backend = "base"


    def make_store(self):
        raise NotImplementedError

    # ── the shared behavioral contract ────────────────────────
    def test_write_recall_roundtrip(self):
        s = self.make_store()
        s.add_knowledge("switched to portone v2", type="decision", layer="P5")
        results = s.recall("portone")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["type"], "decision")
        self.assertIn("who", results[0])

    def test_type_validation(self):
        s = self.make_store()
        with self.assertRaises(ValueError):
            s.add_knowledge("x", type="bogus")

    def test_acl_enforced_and_audited(self):
        s = self.make_store()
        with self.assertRaises(WriteDenied):
            s.add_knowledge("secret", layer="P0", who="agent")
        denied = s.audit_log(denied_only=True)
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0]["action"], "write_denied")

    def test_forget_supersedes(self):
        s = self.make_store()
        kid = s.add_knowledge("payment retry policy", type="pattern")
        self.assertEqual(len(s.recall("payment retry")), 1)
        self.assertTrue(s.forget(kid, reason="obsolete"))
        self.assertEqual(s.recall("payment retry"), [])
        self.assertEqual(s.stats()["active"], 0)

    def test_update_preserves_chain(self):
        s = self.make_store()
        kid = s.add_knowledge("use portone v2", type="decision")
        new = s.update_knowledge(kid, content="use portone v3", confidence=0.9)
        results = s.recall("portone")
        self.assertEqual(len(results), 1)
        self.assertIn("v3", results[0]["content"])
        self.assertEqual(results[0]["confidence"], 0.9)
        # superseded_by chain is asserted backend-specifically
        # (SQLite: test_store; PG: test_pgstore)

    def test_snapshot_rollback(self):
        s = self.make_store()
        before = s.stats()["active"]
        s.snapshot_create("v1")
        s.add_knowledge("later entry", type="fact")
        n = s.snapshot_rollback("v1")
        self.assertEqual(n, 1)
        self.assertEqual(s.stats()["active"], before)
        self.assertEqual(s.recall("later entry"), [])

    def test_confidence_ranks_higher(self):
        s = self.make_store()
        low = s.add_knowledge("the payment gateway retries on failure", type="pattern", confidence=0.2)
        high = s.add_knowledge("the payment gateway retries with exponential backoff per provider docs",
                               type="pattern", confidence=1.0)
        results = s.recall("payment gateway", limit=5)
        self.assertEqual([r["id"] for r in results], [high, low])

    def test_rules_and_assemble(self):
        s = self.make_store()
        s.add_rule("never expose secrets", priority=10, layer="P0")
        s.add_rule("be concise", priority=200, layer="P1")
        out = s.assemble(budget_chars=10000)
        self.assertLess(out.index("P0"), out.index("P1"))

    def test_entities_relations_constraints(self):
        s = self.make_store()
        tool = s.add_entity("portone", "tool")
        incident = s.add_entity("deploy-failed", "incident")
        with self.assertRaises(ValueError):
            s.add_relation(tool, incident, "fixed_by")  # tool not a valid source
        s.add_relation(incident, tool, "references")
        self.assertEqual(s.stats()["relations"], 1)

    def test_graph_trace_and_rca(self):
        s = self.make_store()
        i = s.add_entity("deploy-failed", "incident")
        p = s.add_entity("retry-policy", "pattern")
        s.add_relation(i, p, "fixed_by")
        s.add_relation(p, i, "caused")
        trace = s.graph_trace("deploy-failed")
        self.assertEqual(len(trace["trace"]), 1)
        in_rels = {(r["relation"], r["label"]) for r in trace["trace"][0]["inbound"]}
        out_rels = {(r["relation"], r["label"]) for r in trace["trace"][0]["outbound"]}
        self.assertIn(("caused", "retry-policy"), in_rels)
        self.assertIn(("fixed_by", "retry-policy"), out_rels)
        rca = s.graph_rca("deploy-failed")
        self.assertEqual(len(rca["root_causes"]), 1)

    def test_contradictions(self):
        s = self.make_store()
        s.add_rule("never expose secrets in logs", priority=10)
        s.add_rule("never expose secrets", priority=100)
        s.add_knowledge("client pays via portone v2", type="fact", layer="P5")
        s.add_knowledge("client pays via portone v2", type="decision", layer="P6", who="agent")
        kinds = [c["kind"] for c in s.contradictions()]
        self.assertIn("conflicting_rules", kinds)
        self.assertIn("cross_layer_duplicate", kinds)

    def test_stats_shape(self):
        s = self.make_store()
        s.add_knowledge("a", type="fact", layer="P5")
        stats = s.stats()
        self.assertEqual(stats["by_layer"], {"P5": 1})
        self.assertEqual(stats["active"], 1)
        self.assertIn("audit", stats)

    def test_recall_nothing_found(self):
        s = self.make_store()
        s.add_knowledge("deploy pipeline is green", type="fact")
        self.assertEqual(s.recall("portone"), [])


class SqliteParityTests(BackendParityBase, unittest.TestCase):
    backend = "sqlite"

    def make_store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        s = Store(str(Path(tmp.name) / "memory.db"), embedder=NoopEmbedder())
        self.addCleanup(s.close)
        return s


class PgParityTests(BackendParityBase, unittest.TestCase):
    backend = "postgresql"

    def make_store(self):
        from p_layer.pgstore import PgStore

        try:
            s = PgStore(TEST_DSN, embedder=NoopEmbedder())
        except Exception as exc:
            self.skipTest(f"postgres unavailable: {exc}")
        self.addCleanup(s.close)
        s._execute(
            "TRUNCATE knowledge, episodes, entities, relations, rules, snapshots, audit_log "
            "RESTART IDENTITY CASCADE"
        )
        return s


if __name__ == "__main__":
    unittest.main()
