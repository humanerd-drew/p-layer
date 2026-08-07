import tempfile
import unittest
from pathlib import Path

from p_layer.embed import HashEmbedder
from p_layer.store import Store


class StoreTests(unittest.TestCase):
    def _store(self, **kw):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        kw.setdefault("embedder", HashEmbedder())
        store = Store(str(Path(tmp.name) / "memory.db"), **kw)
        self.addCleanup(store.close)
        return store

    def test_remember_recall_roundtrip(self):
        s = self._store()
        s.add_knowledge("switched to portone v2 for payments", type="decision")
        results = s.recall("portone")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["type"], "decision")
        self.assertIn("portone", results[0]["content"])

    def test_type_validation(self):
        s = self._store()
        with self.assertRaises(ValueError):
            s.add_knowledge("x", type="not-a-type")

    def test_empty_content_rejected(self):
        s = self._store()
        with self.assertRaises(ValueError):
            s.add_knowledge("   ")

    def test_recall_nothing_found(self):
        s = self._store()
        s.add_knowledge("deploy pipeline is green")
        self.assertEqual(s.recall("portone"), [])

    def test_fts_multiword(self):
        s = self._store()
        s.add_knowledge("the payment gateway is down again", type="pattern", source="ops")
        results = s.recall("payment gateway")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source"], "ops")

    def test_rules_precedence_and_budget(self):
        s = self._store()
        s.add_rule("P0: never expose secrets", priority=10, layer="P0")
        s.add_rule("P1: be concise", priority=200, layer="P1")
        out = s.assemble(budget_chars=10000)
        self.assertLess(out.index("P0"), out.index("P1"))

        small = s.assemble(budget_chars=20)
        self.assertLessEqual(len(small), 20)

    def test_entities_and_relations_constraints(self):
        s = self._store()
        portone = s.add_entity("portone", "tool")
        deploy = s.add_entity("deploy-failed", "incident")
        with self.assertRaises(ValueError):
            s.add_relation(portone, deploy, "fixed_by")  # tool not allowed as incident source? fixed_by: incident -> pattern/decision
        s.add_relation(deploy, portone, "references")  # references: any -> any
        self.assertEqual(s.stats()["relations"], 1)

    def test_episodes_append_only_api(self):
        s = self._store()
        s.record_episode("incident", {"what": "deploy failed"}, session_id="s1")
        s.record_episode("retro", "root cause: missing healthcheck")
        self.assertEqual(s.stats()["episodes"], 2)
        with self.assertRaises(ValueError):
            s.record_episode("bogus", "x")

    def test_stats_counts(self):
        s = self._store()
        s.add_knowledge("a", type="fact")
        s.add_knowledge("b", type="decision")
        stats = s.stats()
        self.assertEqual(stats["knowledge"], 2)
        self.assertEqual(stats["by_type"], {"fact": 1, "decision": 1})
        self.assertEqual(stats["embeddings"], 2)  # HashEmbedder ran
        self.assertEqual(stats["schema_version"], 4)

    def test_assemble_includes_recent_knowledge(self):
        s = self._store()
        s.add_knowledge("client prefers weekly sync calls", type="preference")
        out = s.assemble()
        self.assertIn("weekly sync", out)


if __name__ == "__main__":
    unittest.main()
