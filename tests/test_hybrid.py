import tempfile
import unittest
from pathlib import Path

from p_layer.embed import HashEmbedder, NoopEmbedder
from p_layer.store import Store


class HybridRecallTests(unittest.TestCase):
    def _store(self, embedder):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(str(Path(tmp.name) / "memory.db"), embedder=embedder)
        self.addCleanup(store.close)
        return store

    def test_semantic_path_runs_without_network(self):
        # HashEmbedder is NOT semantic, so hybrid defaults to FTS-only;
        # an explicit use_semantic=True still runs the fusion path without
        # network and must not crash.
        s = self._store(HashEmbedder())
        s.add_knowledge("moved the billing service to kubernetes", type="decision")
        s.add_knowledge("portone v2 webhook signature verification", type="pattern")
        results = s.recall("kubernetes billing", limit=5, use_semantic=True)
        self.assertGreaterEqual(len(results), 1)
        self.assertTrue(all("score" in r for r in results))
        self.assertTrue(all(r["type"] in ("decision", "pattern") for r in results))

    def test_hash_embedder_defaults_to_fts_only(self):
        # Benchmark evidence (LongMemEval, hash embedder): fusing pseudo-random
        # vectors as "semantic" scores degrades recall vs FTS-only, so the
        # default must not fuse them.
        s = self._store(HashEmbedder())
        s.add_knowledge("the payment gateway switched to portone v2", type="decision")
        results = s.recall("what payment gateway did we switch to?", limit=5)
        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0]["semantic_score"])

    def test_semantic_disabled_falls_back_to_fts(self):
        s = self._store(NoopEmbedder())
        s.add_knowledge("the deploy failed because of a missing env var", type="pattern")
        results = s.recall("deploy env var")
        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0]["semantic_score"])

    def test_fts_query_with_trailing_question_mark(self):
        # A trailing "?" used to make the whole FTS MATCH raise, silently
        # degrading to a never-matching single-phrase LIKE (zero recall for
        # real agent queries like "did we fix the payment bug?").
        s = self._store(NoopEmbedder())
        s.add_knowledge("the payment gateway switched to portone v2", type="decision")
        results = s.recall("what payment gateway did we switch to?", limit=5)
        self.assertEqual(len(results), 1)
        self.assertIn("portone", results[0]["content"])

    def test_fts_query_with_syntax_chars_and_keyword_terms(self):
        # FTS5 query-syntax chars (: - " *) and keyword collisions
        # (OR/AND/NOT/NEAR) must not break MATCH either.
        s = self._store(NoopEmbedder())
        s.add_knowledge("rule: never expose api keys in logs", type="pattern")
        s.add_knowledge("or and not near are fts5 keywords", type="fact")
        for q in ("never expose api keys?", "or and not near", "rule: orchestration"):
            self.assertGreaterEqual(len(s.recall(q, limit=5)), 1)

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

    def test_homogeneous_corpus_not_starved_by_cap(self):
        # Regression: imported drewgent memory maps every entry to 'fact', so a
        # per-type diversification cap would starve recall below the limit.
        s = self._store(NoopEmbedder())
        for i in range(6):
            s.add_knowledge(f"marketing strategy document {i} b2b analysis", type="fact")
        results = s.recall("marketing b2b", limit=5)
        self.assertEqual(len(results), 5)  # full limit, no cap starvation
        self.assertTrue(all(r["type"] == "fact" for r in results))
    def test_rrf_fusion_prefers_dual_matches(self):
        s = self._store(HashEmbedder())
        s.add_knowledge("portone webhook retry policy for payments", type="pattern")
        s.add_knowledge("the shopping cart totals the order", type="fact")
        results = s.recall("portone payments", limit=3, use_semantic=True)
        self.assertEqual(results[0]["type"], "pattern")


if __name__ == "__main__":
    unittest.main()
