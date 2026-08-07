"""Ops job tests: re-embed (versioned) and consolidation (episodic -> semantic)."""
import tempfile
import unittest
from pathlib import Path

from p_layer.embed import HashEmbedder, NoopEmbedder
from p_layer.store import Store


def _store(self, embedder):
    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)
    s = Store(str(Path(tmp.name) / "memory.db"), embedder=embedder)
    self.addCleanup(s.close)
    return s


class ReembedTests(unittest.TestCase):
    def test_reembed_idempotent(self):
        s = _store(self, HashEmbedder())
        s.add_knowledge("alpha payment fact", type="fact")
        s.add_knowledge("beta deploy pattern", type="pattern")
        r = s.reembed()
        self.assertEqual(r["total"], 2)
        self.assertEqual(r["already"], 2)  # embedded on write
        self.assertEqual(r["embedded"], 0)
        self.assertEqual(s.stats()["embeddings_by_version"], {"hash-64": 2})

    def test_reembed_after_model_switch(self):
        s = _store(self, HashEmbedder())
        s.add_knowledge("alpha payment fact", type="fact")
        s.add_knowledge("beta deploy pattern", type="pattern")
        s.embedder = HashEmbedder(dimensions=128)  # model switch -> new version
        r = s.reembed()
        self.assertEqual(r["embedded"], 2)
        self.assertEqual(r["already"], 0)
        by_version = s.stats()["embeddings_by_version"]
        self.assertEqual(by_version, {"hash-64": 2, "hash-128": 2})  # old version kept

    def test_reembed_skips_superseded(self):
        s = _store(self, HashEmbedder())
        kid = s.add_knowledge("old policy", type="decision")
        s.forget(kid)
        s.embedder = HashEmbedder(dimensions=128)
        r = s.reembed()
        self.assertEqual(r["total"], 0)  # only active knowledge is embedded

    def test_reembed_unavailable_embedder(self):
        s = _store(self, NoopEmbedder())
        s.add_knowledge("fact", type="fact")
        r = s.reembed()
        self.assertIn("reason", r)
        self.assertEqual(r["embedded"], 0)

    def test_reembed_dry_run(self):
        s = _store(self, HashEmbedder())
        kid = s.add_knowledge("fact", type="fact")
        s.db.execute("DELETE FROM embeddings WHERE knowledge_id = ?", (kid,))
        s.db.commit()
        r = s.reembed(dry_run=True)
        self.assertEqual(r["embedded"], 0)
        self.assertEqual(r["dry_run"], True)
        self.assertEqual(s.stats()["embeddings"], 0)  # nothing written


class ConsolidationTests(unittest.TestCase):
    def _seed_episodes(self, s):
        for sid, n in (("s1", 3), ("s2", 2), (None, 1)):
            for i in range(n):
                s.record_episode(
                    "incident",
                    {"title": f"incident {sid or 'orphan'}-{i}", "content": "details"},
                    session_id=sid,
                )

    def test_consolidate_groups_and_digests(self):
        s = _store(self, NoopEmbedder())
        self._seed_episodes(s)
        r = s.consolidate(min_episodes=2)
        # s1: 3 eps -> 1 digest; s2: 2 eps -> 1 digest; orphan: 1 ep -> skipped
        self.assertEqual(r["digests"], 2)
        self.assertEqual(r["episodes_covered"], 5)
        self.assertEqual(r["skipped"], 1)

        results = s.recall("consolidated", limit=10)
        self.assertGreaterEqual(len(results), 2)
        self.assertTrue(all(x["layer"] == "P5" and x["type"] == "insight" for x in results))
        self.assertTrue(all("consolidation:s" in (x["source"] or "") for x in results))

        # idempotent: second run finds nothing
        r2 = s.consolidate(min_episodes=2)
        self.assertEqual(r2["digests"], 0)
        self.assertEqual(r2["episodes_covered"], 0)

    def test_consolidate_dry_run_writes_nothing(self):
        s = _store(self, NoopEmbedder())
        self._seed_episodes(s)
        r = s.consolidate(min_episodes=2, dry_run=True)
        self.assertEqual(r["digests"], 2)
        self.assertEqual(r["dry_run"], True)
        self.assertEqual(s.stats()["knowledge"], 0)
        self.assertEqual(s.stats()["episodes"], 6)  # none marked

    def test_consolidate_custom_summarizer(self):
        s = _store(self, NoopEmbedder())
        for i in range(3):
            s.record_episode("incident", {"title": f"boom {i}"}, session_id="s1")
        r = s.consolidate(min_episodes=2, summarizer=lambda texts, key: "LLM digest here")
        self.assertEqual(r["digests"], 1)
        results = s.recall("LLM digest", limit=5)
        self.assertEqual(len(results), 1)
        self.assertIn("LLM digest here", results[0]["content"])

    def test_consolidation_audited(self):
        s = _store(self, NoopEmbedder())
        for i in range(3):
            s.record_episode("incident", {"title": f"boom {i}"}, session_id="s1")
        s.consolidate(min_episodes=2)
        actions = [e["action"] for e in s.audit_log(limit=5)]
        self.assertIn("consolidate", actions)


class MigrationColumnTests(unittest.TestCase):
    def test_episodes_have_consolidated_at(self):
        s = _store(self, NoopEmbedder())
        s.record_episode("event", "hello")
        cols = {r[1] for r in s.db.execute("PRAGMA table_info(episodes)")}
        self.assertIn("consolidated_at", cols)
        self.assertIn("schema_version", s.stats())
        self.assertEqual(s.stats()["schema_version"], 4)


if __name__ == "__main__":
    unittest.main()
