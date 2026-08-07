import tempfile
import unittest
from pathlib import Path

from memcore.embed import HashEmbedder, NoopEmbedder
from memcore.store import Store


class HybridRecallTests(unittest.TestCase):
    def _store(self, embedder):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(str(Path(tmp.name) / "memory.db"), embedder=embedder)
        self.addCleanup(store.close)
        return store

    def test_semantic_path_runs_without_network(self):
        s = self._store(HashEmbedder())
        s.add_knowledge("moved the billing service to kubernetes", type="decision")
        s.add_knowledge("portone v2 webhook signature verification", type="pattern")
        results = s.recall("kubernetes billing", limit=5)
        # deterministic hash vectors + RRF: must return something, no crash
        self.assertGreaterEqual(len(results), 1)
        self.assertTrue(all("score" in r for r in results))
        self.assertTrue(all(r["type"] in ("decision", "pattern") for r in results))

    def test_semantic_disabled_falls_back_to_fts(self):
        s = self._store(NoopEmbedder())
        s.add_knowledge("the deploy failed because of a missing env var", type="pattern")
        results = s.recall("deploy env var")
        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0]["semantic_score"])

    def test_type_diversification_caps_per_type(self):
        s = self._store(NoopEmbedder())
        for i in range(6):
            s.add_knowledge(f"payment retry number {i}", type="pattern")
        for i in range(4):
            s.add_knowledge(f"payment quota note {i}", type="fact")
        results = s.recall("payment", limit=10)
        counts = {}
        for r in results:
            counts[r["type"]] = counts.get(r["type"], 0) + 1
        self.assertLessEqual(counts.get("pattern", 0), 3)
        self.assertLessEqual(counts.get("fact", 0), 3)

    def test_rrf_fusion_prefers_dual_matches(self):
        s = self._store(HashEmbedder())
        s.add_knowledge("portone webhook retry policy for payments", type="pattern")
        s.add_knowledge("the shopping cart totals the order", type="fact")
        results = s.recall("portone payments", limit=3)
        self.assertEqual(results[0]["type"], "pattern")


if __name__ == "__main__":
    unittest.main()
