"""v2 layer-governance tests: ACLs, supersede, confidence/TTL ranking, snapshots, serendipity."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from p_layer.embed import NoopEmbedder
from p_layer.mcp import handle_message
from p_layer.migrations import migrate
from p_layer.store import Store, WriteDenied


class MigrationUpgradeTests(unittest.TestCase):
    def test_upgrade_v1_to_v2(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = sqlite3.connect(str(Path(tmp.name) / "m.db"))
        db.row_factory = sqlite3.Row
        migrate(db, target=1)
        db.execute(
            "INSERT INTO knowledge (type, content, created_at) VALUES ('fact','old entry','2026-08-01T00:00:00Z')"
        )
        db.commit()
        migrate(db)  # apply v2
        cols = {r[1] for r in db.execute("PRAGMA table_info(knowledge)")}
        self.assertIn("layer", cols)
        self.assertIn("who", cols)
        self.assertIn("confidence", cols)
        self.assertIn("ttl_days", cols)
        self.assertIn("superseded_by", cols)
        row = db.execute("SELECT layer, who, confidence FROM knowledge WHERE content='old entry'").fetchone()
        self.assertEqual((row[0], row[1], row[2]), ("P5", "system", 1.0))
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("snapshots", tables)
        db.close()


class LayerAclTests(unittest.TestCase):
    def _store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(str(Path(tmp.name) / "memory.db"), embedder=NoopEmbedder())
        self.addCleanup(store.close)
        return store

    def test_p0_system_only(self):
        s = self._store()
        with self.assertRaises(WriteDenied):
            s.add_knowledge("secret rule", layer="P0", who="agent")
        s.add_knowledge("system rule", layer="P0", who="system")  # allowed

    def test_p1_human_only(self):
        s = self._store()
        with self.assertRaises(WriteDenied):
            s.add_knowledge("persona", layer="P1", who="agent")
        # p-layer README says "P1: human only", but its LAYER_WRITERS code says
        # system-only — we ported the code, not the prose.
        with self.assertRaises(WriteDenied):
            s.add_knowledge("persona", layer="P1", who="manual")
        s.add_knowledge("persona", layer="P1", who="system")

    def test_tool_allowed_on_p5(self):
        s = self._store()
        s.add_knowledge("fact", layer="P5", who="tool:p_layer")  # principal 'tool' allowed

    def test_principal_prefix_extraction(self):
        s = self._store()
        with self.assertRaises(WriteDenied):
            s.add_knowledge("x", layer="P0", who="agent:deep-thought")  # 'agent' denied on P0

    def test_invalid_layer_rejected(self):
        s = self._store()
        with self.assertRaises(ValueError):
            s.add_knowledge("x", layer="P9", who="system")


class SupersedeTests(unittest.TestCase):
    def _store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(str(Path(tmp.name) / "memory.db"), embedder=NoopEmbedder())
        self.addCleanup(store.close)
        return store

    def test_forget_supersedes_not_deletes(self):
        s = self._store()
        kid = s.add_knowledge("payment retry policy is exponential", type="pattern")
        self.assertEqual(len(s.recall("payment retry")), 1)
        self.assertTrue(s.forget(kid, reason="new policy"))
        self.assertEqual(s.recall("payment retry"), [])
        # row still exists, history preserved
        stats = s.stats()
        self.assertEqual(stats["knowledge"], 1)
        self.assertEqual(stats["active"], 0)
        self.assertFalse(s.forget(kid))  # already superseded

    def test_update_supersedes_and_preserves_chain(self):
        s = self._store()
        kid = s.add_knowledge("use portone v2", type="decision")
        new = s.update_knowledge(kid, content="use portone v3", confidence=0.9)
        results = s.recall("portone")
        self.assertEqual(len(results), 1)
        self.assertIn("v3", results[0]["content"])
        self.assertEqual(results[0]["confidence"], 0.9)
        row = s.db.execute("SELECT superseded_by FROM knowledge WHERE id=?", (kid,)).fetchone()
        self.assertEqual(row["superseded_by"], new)
        with self.assertRaises(ValueError):
            s.update_knowledge(kid, content="x")  # already superseded

    def test_update_unknown_id(self):
        s = self._store()
        with self.assertRaises(ValueError):
            s.update_knowledge(9999, content="x")


class RankingTests(unittest.TestCase):
    def _store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(str(Path(tmp.name) / "memory.db"), embedder=NoopEmbedder())
        self.addCleanup(store.close)
        return store

    def test_confidence_ranks_higher(self):
        s = self._store()
        low = s.add_knowledge("the payment gateway retries", type="pattern", confidence=0.2)
        high = s.add_knowledge("the payment gateway retries", type="pattern", confidence=1.0)
        results = s.recall("payment gateway", limit=5)
        self.assertEqual([r["id"] for r in results], [high, low])

    def test_ttl_freshness_ranks_fresh_first(self):
        s = self._store()
        old = s.add_knowledge(
            "portone webhook status check", type="pattern", ttl_days=1,
            created_at="2026-01-01T00:00:00Z",
        )
        new = s.add_knowledge("portone webhook status check", type="pattern", ttl_days=1)
        results = s.recall("portone webhook", limit=5)
        self.assertEqual(results[0]["id"], new)
        self.assertLess(results[1]["score"], results[0]["score"])

    def test_serendipity_off_is_deterministic(self):
        s = self._store()
        s.add_knowledge("alpha payment retry", type="pattern")
        s.add_knowledge("beta unrelated topic", type="fact")
        r1 = s.recall("alpha payment", serendipity=False)
        r2 = s.recall("alpha payment", serendipity=False)
        self.assertEqual([x["id"] for x in r1], [x["id"] for x in r2])
        self.assertFalse(any(x.get("_serendipity") for x in r1))

    def test_serendipity_path_never_crashes(self):
        s = self._store()
        s.add_knowledge("alpha payment retry", type="pattern")
        s.add_knowledge("beta unrelated topic", type="fact")
        for _ in range(40):  # exercise the 5% branch repeatedly
            results = s.recall("alpha payment", serendipity=True)
            self.assertGreaterEqual(len(results), 1)


class SnapshotTests(unittest.TestCase):
    def _store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(str(Path(tmp.name) / "memory.db"), embedder=NoopEmbedder())
        self.addCleanup(store.close)
        return store

    def test_snapshot_rollback_supersedes_only_after(self):
        s = self._store()
        keep = s.add_knowledge("keep me", type="fact")
        before = s.stats()["active"]
        s.snapshot_create("v1")
        s.add_knowledge("later entry", type="fact")
        self.assertEqual(s.stats()["active"], before + 1)
        n = s.snapshot_rollback("v1")
        self.assertEqual(n, 1)
        self.assertEqual(s.stats()["active"], before)
        self.assertEqual(s.recall("later entry"), [])
        self.assertEqual(len(s.recall("keep me")), 1)
        # pre-snapshot entries untouched
        row = s.db.execute("SELECT superseded_by FROM knowledge WHERE id=?", (keep,)).fetchone()
        self.assertIsNone(row["superseded_by"])

    def test_snapshot_unknown_version(self):
        s = self._store()
        with self.assertRaises(ValueError):
            s.snapshot_rollback("nope")

    def test_stats_layer_counts(self):
        s = self._store()
        s.add_knowledge("a", type="fact", layer="P5", who="tool:x")
        s.add_knowledge("b", type="decision", layer="P6", who="agent")
        stats = s.stats()
        self.assertEqual(stats["by_layer"], {"P5": 1, "P6": 1})
        self.assertEqual(stats["active"], 2)


class McpLayerTests(unittest.TestCase):
    def _store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(str(Path(tmp.name) / "memory.db"), embedder=NoopEmbedder())
        self.addCleanup(store.close)
        return store

    def _call(self, s, tool, arguments, msg_id=1):
        return handle_message(
            {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
             "params": {"name": tool, "arguments": arguments}},
            s,
        )

    def test_mcp_forget_update_snapshot_roundtrip(self):
        s = self._store()
        r = self._call(s, "remember", {"fact": "use portone v2", "type": "decision"})
        self.assertIn("saved #1", r["result"]["content"][0]["text"])

        r = self._call(s, "update", {"id": 1, "fact": "use portone v3"}, msg_id=2)
        self.assertIn("→ #2", r["result"]["content"][0]["text"])

        r = self._call(s, "snapshot_create", {"version_id": "v1"}, msg_id=3)
        self.assertIn("created", r["result"]["content"][0]["text"])

        self._call(s, "remember", {"fact": "created after snapshot"}, msg_id=4)
        r = self._call(s, "snapshot_rollback", {"version_id": "v1"}, msg_id=5)
        self.assertIn("superseded", r["result"]["content"][0]["text"])
        self.assertEqual(s.recall("created after snapshot"), [])

        r = self._call(s, "forget", {"id": 2, "reason": "obsolete"}, msg_id=6)
        self.assertIn("superseded #2", r["result"]["content"][0]["text"])
        self.assertEqual(s.recall("portone"), [])

    def test_mcp_remember_respects_layer_acl(self):
        s = self._store()
        r = self._call(s, "remember", {"fact": "x", "layer": "P0"})  # who=tool:p_layer, denied on P0
        self.assertIn("error", r)
        self.assertIn("does not allow", r["error"]["message"])


if __name__ == "__main__":
    unittest.main()
