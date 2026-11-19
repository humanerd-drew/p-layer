"""Regression: update/forget/rollback must authorize the *caller*, not the entry's original author.

Before the fix, an MCP client could not write to P0 (denied), but could `update` or
`forget` an existing system-written P0 entry, because update reused the stored author
("system") as the caller and forget had no layer check at all.
"""
import tempfile
import unittest
from pathlib import Path

from p_layer.embed import NoopEmbedder
from p_layer.mcp import handle_message
from p_layer.store import Store, WriteDenied


class ProtectedLayerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.s = Store(str(Path(tmp.name) / "m.db"), embedder=NoopEmbedder())
        self.addCleanup(self.s.close)
        self.rule = self.s.add_knowledge("NEVER edit files under /protected", type="decision",
                                         layer="P0", who="system")

    def _mcp(self, tool, args):
        return handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": tool, "arguments": args}}, self.s)

    def _active(self, entry_id):
        row = self.s.db.execute("SELECT superseded_by FROM knowledge WHERE id=?", (entry_id,)).fetchone()
        return row["superseded_by"] is None

    def _denied(self):
        return [e for e in self.s.audit_log(limit=50) if e["denied"]]

    # --- store level ---
    def test_non_system_cannot_update_p0(self):
        with self.assertRaises(WriteDenied):
            self.s.update_knowledge(self.rule, content="edits are fine", who="tool:p_layer")
        self.assertTrue(self._active(self.rule))
        self.assertEqual(len(self._denied()), 1)

    def test_non_system_cannot_forget_p0(self):
        with self.assertRaises(WriteDenied):
            self.s.forget(self.rule, who="agent")
        self.assertTrue(self._active(self.rule))
        self.assertEqual(len(self._denied()), 1)

    def test_system_can_update_and_forget_p0(self):
        new = self.s.update_knowledge(self.rule, content="NEVER edit /protected or /secrets", who="system")
        self.assertFalse(self._active(self.rule))
        self.assertTrue(self.s.forget(new, who="system"))

    def test_default_caller_is_system_for_library_use(self):
        # Programmatic API keeps its old behavior: omitted `who` means system.
        self.assertTrue(self.s.forget(self.rule))

    def test_update_cannot_move_entry_into_p0(self):
        kid = self.s.add_knowledge("a note", layer="P5", who="tool:p_layer")
        with self.assertRaises(WriteDenied):
            self.s.update_knowledge(kid, content="now a rule", layer="P0", who="tool:p_layer")
        self.assertTrue(self._active(kid))

    def test_rollback_skips_layers_caller_cannot_write(self):
        self.s.snapshot_create("v1")
        p0_after = self.s.add_knowledge("new rule after snapshot", layer="P0", who="system")
        p5_after = self.s.add_knowledge("note after snapshot", layer="P5", who="tool:p_layer")
        n = self.s.snapshot_rollback("v1", who="tool:p_layer")
        self.assertEqual(n, 1)
        self.assertTrue(self._active(p0_after))
        self.assertFalse(self._active(p5_after))

    def test_rollback_as_system_still_covers_all_layers(self):
        self.s.snapshot_create("v1")
        p0_after = self.s.add_knowledge("new rule after snapshot", layer="P0", who="system")
        self.s.add_knowledge("note after snapshot", layer="P5", who="tool:p_layer")
        self.assertEqual(self.s.snapshot_rollback("v1"), 2)
        self.assertFalse(self._active(p0_after))

    # --- MCP level (the path an agent actually uses) ---
    def test_mcp_update_p0_denied(self):
        r = self._mcp("update", {"id": self.rule, "fact": "edits under /protected are fine"})
        self.assertIn("error", r)
        self.assertIn("does not allow", r["error"]["message"])
        self.assertTrue(self._active(self.rule))

    def test_mcp_forget_p0_denied(self):
        r = self._mcp("forget", {"id": self.rule, "reason": "x"})
        self.assertIn("error", r)
        self.assertTrue(self._active(self.rule))

    def test_mcp_rollback_keeps_p0(self):
        self._mcp("snapshot_create", {"version_id": "v1"})
        p0_after = self.s.add_knowledge("late rule", layer="P0", who="system")
        r = self._mcp("snapshot_rollback", {"version_id": "v1"})
        self.assertTrue(self._active(p0_after))
        self.assertIn("1 protected entry(ies) left unchanged", r["result"]["content"][0]["text"])
        audit = [e for e in self.s.audit_log(limit=20) if e["action"] == "snapshot_rollback"][0]
        self.assertIn("skipped_protected=1", audit["detail"])

    def test_mcp_ordinary_p5_update_forget_still_work(self):
        r = self._mcp("remember", {"fact": "use portone v2", "type": "decision"})
        self.assertIn("saved", r["result"]["content"][0]["text"])
        kid = int(r["result"]["content"][0]["text"].split("#")[1].split()[0])
        r = self._mcp("update", {"id": kid, "fact": "use portone v3"})
        self.assertIn("updated", r["result"]["content"][0]["text"])
        r = self._mcp("forget", {"id": kid + 1})
        self.assertIn("superseded", r["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
