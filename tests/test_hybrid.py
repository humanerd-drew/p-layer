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


class AdditiveRerankTests(unittest.TestCase):
    """Tests for the additive reranker (RerankConfig + _rerank_additive)."""

    def test_rerank_components_present_in_results(self):
        """When rerank is enabled (default), results include rerank_components."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        s = Store(str(Path(tmp.name) / "memory.db"), embedder=NoopEmbedder())
        self.addCleanup(s.close)
        s.add_knowledge("kubernetes deployment strategy blue-green", type="decision")
        results = s.recall("kubernetes")
        self.assertEqual(len(results), 1)
        self.assertIn("rerank_components", results[0])
        self.assertIn("rrf", results[0]["rerank_components"])
        self.assertIn("confidence_boost", results[0]["rerank_components"])
        self.assertIn("recency_boost", results[0]["rerank_components"])

    def test_higher_confidence_ranks_higher(self):
        """Given equal RRF, higher confidence entry should rank first."""
        from p_layer.store import rrf_fuse, RerankConfig

        row_high = {"id": 1, "type": "fact", "content": "high", "source": "test",
                    "layer": "P5", "who": "system", "confidence": 0.9,
                    "created_at": "2026-08-20T12:00:00+00:00", "ttl_days": None}
        row_low = {"id": 2, "type": "fact", "content": "low", "source": "test",
                   "layer": "P5", "who": "system", "confidence": 0.1,
                   "created_at": "2026-08-20T12:00:00+00:00", "ttl_days": None}

        # Both in FTS at same rank positions — simulate by giving them both rank 1
        fts = [row_high, row_low]
        sem: list[tuple[float, int]] = []
        lookup = lambda ids: {1: row_high, 2: row_low}

        results = rrf_fuse(fts, sem, 10, lookup, rerank=RerankConfig())
        self.assertEqual(results[0]["id"], 1)
        self.assertEqual(results[1]["id"], 2)

    def test_recency_boost_favors_newer_entries(self):
        """Within recency window, newer entry gets a higher boost."""
        from p_layer.store import rrf_fuse, RerankConfig

        row_new = {"id": 1, "type": "fact", "content": "new entry", "source": "test",
                   "layer": "P5", "who": "system", "confidence": 0.5,
                   "created_at": "2026-08-20T12:00:00+00:00", "ttl_days": None}
        row_old = {"id": 2, "type": "fact", "content": "old entry", "source": "test",
                   "layer": "P5", "who": "system", "confidence": 0.5,
                   "created_at": "2025-01-01T12:00:00+00:00", "ttl_days": None}

        fts = [row_old, row_new]  # FTS gives old first (wrong order)
        sem: list[tuple[float, int]] = []
        lookup = lambda ids: {1: row_new, 2: row_old}

        results = rrf_fuse(fts, sem, 10, lookup,
                           rerank=RerankConfig(recency_window_days=30.0))
        # Old is outside 30-day window (gets 0 recency boost)
        # New gets recency boost — but both have same confidence (center=0.5, boost=0)
        # RRF rank: old=rank1 (higher rrf), new=rank2 (lower rrf)
        # So recency must overcome the RRF gap; with default gain it's very small.
        # This tests that the component exists and is positive for new, zero for old.
        new_result = next(r for r in results if r["id"] == 1)
        old_result = next(r for r in results if r["id"] == 2)
        self.assertGreater(new_result["rerank_components"]["recency_boost"], 0)
        self.assertEqual(old_result["rerank_components"]["recency_boost"], 0)

    def test_legacy_mode_no_rerank_components(self):
        """With rerank disabled, results do NOT include rerank_components."""
        from p_layer.store import rrf_fuse, RerankConfig

        row = {"id": 1, "type": "fact", "content": "test", "source": "test",
               "layer": "P5", "who": "system", "confidence": 0.8,
               "created_at": "2026-08-20T12:00:00+00:00", "ttl_days": None}

        fts = [row]
        sem: list[tuple[float, int]] = []
        lookup = lambda ids: {1: row}

        results = rrf_fuse(fts, sem, 10, lookup,
                           rerank=RerankConfig(enabled=False))
        self.assertEqual(len(results), 1)
        self.assertNotIn("rerank_components", results[0])

    def test_custom_recency_window(self):
        """Custom recency_window_days is respected."""
        from p_layer.store import rrf_fuse, RerankConfig

        # Entry is 10 days old
        from datetime import datetime, timezone, timedelta
        ten_days_ago = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        row = {"id": 1, "type": "fact", "content": "recent", "source": "test",
               "layer": "P5", "who": "system", "confidence": 0.5,
               "created_at": ten_days_ago, "ttl_days": None}

        fts = [row]
        sem: list[tuple[float, int]] = []
        lookup = lambda ids: {1: row}

        # With 30-day window: 10/30 = 0.33 age → recency = 0.67 * gain
        results_30 = rrf_fuse(fts, sem, 10, lookup,
                              rerank=RerankConfig(recency_window_days=30.0))
        # With 7-day window: 10/7 > 1 → recency = 0
        results_7 = rrf_fuse(fts, sem, 10, lookup,
                             rerank=RerankConfig(recency_window_days=7.0))

        self.assertGreater(results_30[0]["rerank_components"]["recency_boost"], 0)
        self.assertEqual(results_7[0]["rerank_components"]["recency_boost"], 0)


if __name__ == "__main__":
    unittest.main()
