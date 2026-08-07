"""Eval harness tests: recall@k (memcore vs drewgent baseline) + ACL compliance."""
import json
import tempfile
import unittest
from pathlib import Path

from memcore.embed import NoopEmbedder
from memcore.eval import acl_compliance, build_drewgent_baseline, load_suite, recall_at_k, run_eval
from memcore.store import Store


def _store(self, entries):
    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)
    store = Store(str(Path(tmp.name) / "memory.db"), embedder=NoopEmbedder())
    self.addCleanup(store.close)
    for kwargs in entries:
        store.add_knowledge(**kwargs)
    return store


class RecallEvalTests(unittest.TestCase):
    def test_memcore_beats_baseline_on_confidence_ranking(self):
        # Same FTS relevance; baseline returns insertion order (low confidence
        # first), memcore ranks by confidence. Expected entry must be top-1.
        s = _store(self, [
            {"content": "the payment gateway retries on failure", "type": "pattern", "confidence": 0.2},
            {"content": "the payment gateway retries with exponential backoff per provider docs",
             "type": "pattern", "confidence": 1.0},
        ])
        suite = {
            "queries": [
                {"query": "payment gateway", "expected": ["exponential backoff"], "k": 1},
            ]
        }
        result = run_eval(s, suite)
        base = result["recall"]["baseline"]["_total"]
        mem = result["recall"]["memcore"]["_total"]
        self.assertEqual(base["recall@k"], 0.0)   # baseline returns the low-confidence entry
        self.assertEqual(mem["recall@k"], 1.0)   # memcore returns the high-confidence entry
        self.assertGreater(mem["recall@k"], base["recall@k"])

    def test_superseded_entries_absent_from_both_engines(self):
        s = _store(self, [
            {"content": "use portone v2", "type": "decision"},
        ])
        kid = s.db.execute("SELECT id FROM knowledge").fetchone()["id"]
        s.forget(kid)
        suite = {"queries": [{"query": "portone", "expected": ["portone"], "k": 3}]}
        result = run_eval(s, suite)
        # baseline copies only active rows, so both engines miss — correctly.
        self.assertEqual(result["recall"]["baseline"]["_total"]["hits"], 0)
        self.assertEqual(result["recall"]["memcore"]["_total"]["hits"], 0)

    def test_load_suite_validates(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / "suite.json"
        p.write_text(json.dumps({"queries": [{"query": "a", "expected": ["x"]}]}))
        self.assertEqual(load_suite(p)["queries"][0]["query"], "a")
        p.write_text(json.dumps({"queries": []}))
        with self.assertRaises(ValueError):
            load_suite(p)
        p.write_text(json.dumps({"queries": [{"query": "a", "expected": []}]}))
        with self.assertRaises(ValueError):
            load_suite(p)

    def test_baseline_db_is_drewgent_schema(self):
        s = _store(self, [{"content": "hello world", "type": "fact"}])
        base = build_drewgent_baseline(s)
        self.addCleanup(base.close)
        tables = {r[0] for r in base.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("knowledge", tables)
        self.assertIn("knowledge_fts", tables)


class AclComplianceTests(unittest.TestCase):
    def test_full_compliance(self):
        result = acl_compliance()
        self.assertEqual(result["pass_rate"], 1.0)
        self.assertEqual(result["passed"], result["total"])

    def test_every_allowed_writer_passes(self):
        from memcore.store import LAYER_WRITERS, _check_layer_write

        for layer, allowed in LAYER_WRITERS.items():
            for who in allowed:
                _check_layer_write(layer, who)  # must not raise


if __name__ == "__main__":
    unittest.main()
