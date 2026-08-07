"""p-layers 1.0 compat tests: KnowledgeDB API and knowledge_* MCP tools over memcore."""
import json
import tempfile
import unittest
from pathlib import Path

from memcore.mcp import handle_message as memcore_handle  # noqa: F401 (unused; for shape comparison)
from p_layer import KnowledgeDB, WriteDenied
from p_layer.mcp.server import handle_message


def _db(self):
    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)
    db = KnowledgeDB(db_dir=tmp.name)
    self.addCleanup(db.close)
    return db


class KnowledgeDbCompatTests(unittest.TestCase):
    def test_insert_and_search(self):
        db = _db(self)
        result = db.insert(
            layer="P5", type="fact",
            content="P-layer organizes agent memory into 7 governance layers.",
            who="system:test",
        )
        self.assertIsNotNone(result["id"])
        self.assertEqual(result["layer"], "P5")
        self.assertEqual(result["authority"], 30)  # LAYER_AUTHORITY["P5"]

        results = db.search("governance", limit=10)
        self.assertGreaterEqual(len(results), 1)
        self.assertIn("governance", results[0]["content"])

    def test_write_denied_propagates(self):
        db = _db(self)
        with self.assertRaises(WriteDenied):
            db.insert(layer="P0", type="fact", content="secret", who="unauthorized:user")

    def test_invalid_layer(self):
        db = _db(self)
        with self.assertRaises(ValueError):
            db.insert(layer="P99", type="fact", content="bad")

    def test_get_layer_count_real(self):
        db = _db(self)
        db.insert(layer="P5", type="fact", content="a", who="system")
        db.insert(layer="P6", type="pattern", content="b", who="agent")
        self.assertEqual(db.get_layer_count(), {"P5": 1, "P6": 1})

    def test_search_layer_filter(self):
        db = _db(self)
        db.insert(layer="P5", type="fact", content="alpha payment fact", who="system")
        db.insert(layer="P6", type="pattern", content="alpha payment pattern", who="agent")
        p5 = db.search("alpha payment", layers=["P5"])
        self.assertEqual(len(p5), 1)
        self.assertEqual(p5[0]["layer"], "P5")

    def test_graph_query_shape(self):
        db = _db(self)
        self.assertEqual(db.graph_query("nonexistent"), {"nodes": [], "edges": []})
        i = db._store.add_entity("deploy-failed", "incident")
        p = db._store.add_entity("retry-policy", "pattern")
        db._store.add_relation(i, p, "fixed_by")
        result = db.graph_query("deploy-failed")
        self.assertEqual(len(result["nodes"]), 1)
        self.assertEqual(len(result["edges"]), 1)
        self.assertEqual(result["edges"][0]["type"], "fixed_by")

    def test_hybrid_search_and_vector_contract(self):
        db = _db(self)
        db.insert(layer="P5", type="fact", content="hybrid fallback test", who="system")
        self.assertGreaterEqual(len(db.hybrid_search("hybrid")), 1)
        self.assertEqual(db.vector_search([0.1] * 10), [])  # 0.1.x SQLite contract

    def test_context_manager_and_properties(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: None)
        with KnowledgeDB(db_dir=tmp) as kdb:
            r = kdb.insert(layer="P5", type="fact", content="cm test", who="system")
            self.assertIsNotNone(r["id"])
        self.assertTrue(kdb.available)
        self.assertFalse(kdb.has_pg)

    def test_pg_mode_explicitly_deferred(self):
        db = _db(self)
        with self.assertRaises(NotImplementedError):
            db.set_mode("pg")

    def test_search_by_who(self):
        db = _db(self)
        db.insert(layer="P5", type="fact", content="who filter test", who="tool:x")
        self.assertEqual(len(db.search("who filter", who="tool:x")), 1)
        self.assertEqual(db.search("who filter", who="agent"), [])


class McpCompatTests(unittest.TestCase):
    def _call(self, db, tool, arguments, msg_id=1):
        return handle_message(
            {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
             "params": {"name": tool, "arguments": arguments}},
            db,
        )

    def test_initialize_and_tools_list(self):
        db = _db(self)
        resp = handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, db)
        self.assertEqual(resp["result"]["serverInfo"]["name"], "p-layers")
        resp = handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, db)
        names = {t["name"] for t in resp["result"]["tools"]}
        self.assertEqual(names, {
            "knowledge_remember", "knowledge_recall", "knowledge_forget",
            "knowledge_update", "knowledge_memory-stats",
            "knowledge_snapshot-create", "knowledge_snapshot-rollback",
            "knowledge_ontology-status",
        })

    def test_remember_recall_forget_roundtrip(self):
        db = _db(self)
        r = self._call(db, "knowledge_remember", {"fact": "use portone v2", "type": "decision"})
        entry = json.loads(r["result"]["content"][0]["text"])
        self.assertEqual(entry["layer"], "P5")

        r = self._call(db, "knowledge_recall", {"query": "portone"})
        results = json.loads(r["result"]["content"][0]["text"])
        self.assertEqual(len(results), 1)
        self.assertIn("portone", results[0]["content"])

        r = self._call(db, "knowledge_forget", {"id": entry["id"], "reason": "obsolete"})
        self.assertTrue(json.loads(r["result"]["content"][0]["text"])["superseded"])
        self.assertEqual(db.search("portone"), [])

    def test_snapshot_roundtrip(self):
        db = _db(self)
        self._call(db, "knowledge_snapshot-create", {"version_id": "v1"})
        self._call(db, "knowledge_remember", {"fact": "created after snapshot"})
        r = self._call(db, "knowledge_snapshot-rollback", {"version_id": "v1"})
        self.assertEqual(json.loads(r["result"]["content"][0]["text"])["rolled_back"], 1)
        self.assertEqual(db.search("created after snapshot"), [])

    def test_remember_layer_acl_via_mcp(self):
        db = _db(self)
        r = self._call(db, "knowledge_remember", {"fact": "x", "layer": "P0"})  # who=tool denied on P0
        self.assertIn("error", r)
        self.assertIn("does not allow", r["error"]["message"])

    def test_ontology_status(self):
        db = _db(self)
        r = self._call(db, "knowledge_ontology-status", {})
        status = json.loads(r["result"]["content"][0]["text"])
        self.assertIn("entities", status)
        self.assertIn("relations", status)
        self.assertIn("contradictions", status)


if __name__ == "__main__":
    unittest.main()
