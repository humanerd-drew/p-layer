"""Governance evidence tests: audit log, contradiction scan, wiki compile."""
import json
import tempfile
import unittest
from pathlib import Path

from p_layer.embed import NoopEmbedder
from p_layer.store import Store, WriteDenied


def _store(self):
    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)
    store = Store(str(Path(tmp.name) / "memory.db"), embedder=NoopEmbedder())
    self.addCleanup(store.close)
    return store


class AuditLogTests(unittest.TestCase):
    def test_writes_are_audited(self):
        s = _store(self)
        kid = s.add_knowledge("use portone v2", type="decision", layer="P5", who="tool:x")
        new = s.update_knowledge(kid, content="use portone v3")
        s.forget(new, reason="obsolete")
        s.snapshot_create("v1")
        s.snapshot_rollback("v1")
        s.add_rule("never expose secrets", priority=10)

        actions = [e["action"] for e in s.audit_log(limit=50)]
        self.assertIn("remember", actions)
        self.assertIn("forget", actions)
        self.assertIn("update", actions)
        self.assertIn("snapshot_create", actions)
        self.assertIn("snapshot_rollback", actions)
        self.assertIn("rule_add", actions)
        stats = s.stats()
        self.assertGreaterEqual(stats["audit"], 6)

    def test_denied_writes_are_audited(self):
        s = _store(self)
        with self.assertRaises(WriteDenied):
            s.add_knowledge("secret", layer="P0", who="agent")
        denied = s.audit_log(denied_only=True)
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0]["action"], "write_denied")
        self.assertEqual(denied[0]["layer"], "P0")
        self.assertEqual(denied[0]["who"], "agent")
        self.assertIn("does not allow", denied[0]["detail"])

    def test_audit_entry_shape(self):
        s = _store(self)
        s.add_knowledge("fact one", type="fact")
        entry = s.audit_log(limit=1)[0]
        for key in ("id", "action", "knowledge_id", "layer", "who", "detail", "denied", "created_at"):
            self.assertIn(key, entry)
        self.assertEqual(entry["knowledge_id"], 1)


class ContradictionTests(unittest.TestCase):
    def test_conflicting_rules_detected(self):
        s = _store(self)
        s.add_rule("never expose secrets in logs", priority=10)
        s.add_rule("never expose secrets", priority=100)
        found = s.contradictions()
        kinds = [c["kind"] for c in found]
        self.assertIn("conflicting_rules", kinds)

    def test_same_priority_rules_not_conflicting(self):
        s = _store(self)
        s.add_rule("never expose secrets in logs", priority=10)
        s.add_rule("never expose secrets", priority=10)
        found = s.contradictions()
        self.assertNotIn("conflicting_rules", [c["kind"] for c in found])

    def test_cross_layer_duplicate_detected(self):
        s = _store(self)
        s.add_knowledge("client pays via portone v2", type="fact", layer="P5")
        s.add_knowledge("client pays via portone v2", type="decision", layer="P6", who="agent")
        found = s.contradictions()
        self.assertIn("cross_layer_duplicate", [c["kind"] for c in found])

    def test_clean_store_no_contradictions(self):
        s = _store(self)
        s.add_knowledge("deploy pipeline is green", type="fact")
        s.add_knowledge("the cat sat on the mat", type="pattern")
        self.assertEqual(s.contradictions(), [])


class WikiCompileTests(unittest.TestCase):
    def test_compile_wiki_output(self):
        s = _store(self)
        s.add_knowledge("switched to portone v2", type="decision", layer="P5", who="tool:x", source="session: payments")
        s.add_knowledge("client prefers weekly sync", type="preference", layer="P6", who="agent")
        s.add_rule("never expose secrets", priority=10)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        result = s.compile_wiki(tmp.name)

        root = Path(tmp.name)
        self.assertTrue((root / "INDEX.md").exists())
        self.assertTrue((root / "P5-ego" / "wiki" / "compiled" / "P5.md").exists())
        self.assertTrue((root / "P5-ego" / "wiki" / "compiled" / "P6.md").exists())
        self.assertTrue((root / "P5-ego" / "wiki" / "compiled" / "rules.md").exists())

        p5 = (root / "P5-ego" / "wiki" / "compiled" / "P5.md").read_text()
        self.assertIn("switched to portone v2", p5)
        self.assertIn("session: payments", p5)
        self.assertIn("tool:x", p5)

        index = (root / "INDEX.md").read_text()
        self.assertIn("P5.md", index)
        self.assertGreaterEqual(result["entries"], 3)

    def test_compile_wiki_superseded_excluded(self):
        s = _store(self)
        kid = s.add_knowledge("old policy", type="decision")
        s.forget(kid)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        result = s.compile_wiki(tmp.name)
        self.assertEqual(result["entries"], 0)
        self.assertFalse((Path(tmp.name) / "P5-ego" / "wiki" / "compiled" / "P5.md").exists())


if __name__ == "__main__":
    unittest.main()
