"""Regression: update/forget/rollback must authorize the *caller*, not the entry's original author.

Before the fix, an MCP client could not write to P0 (denied), but could `update` or
`forget` an existing system-written P0 entry, because update reused the stored author
("system") as the caller and forget had no layer check at all.
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from p_layer.cli import main as cli_main
from p_layer.compat import KnowledgeDB
from p_layer.compat.mcp import handle_message as compat_handle_message
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

    def test_default_system_can_promote_tool_entry_to_p0(self):
        kid = self.s.add_knowledge("tentative rule", layer="P5", who="tool:p_layer")
        new_id = self.s.update_knowledge(kid, content="promoted rule", layer="P0")
        self.assertFalse(self._active(kid))
        self.assertTrue(self._active(new_id))
        row = self.s.db.execute("SELECT layer, who FROM knowledge WHERE id=?", (new_id,)).fetchone()
        self.assertEqual(row["layer"], "P0")
        self.assertEqual(row["who"], "system")
        audit_for_update = [
            e for e in self.s.audit_log(limit=20)
            if (e["action"] == "update" and e["knowledge_id"] == kid)
            or (e["action"] == "remember" and e["knowledge_id"] == new_id)
        ]
        self.assertEqual(len(audit_for_update), 2)
        self.assertTrue(all(e["who"] == "system" for e in audit_for_update))

    def test_empty_who_cannot_escalate_to_system(self):
        with self.assertRaises(WriteDenied):
            self.s.update_knowledge(self.rule, content="bypass", who="")
        with self.assertRaises(WriteDenied):
            self.s.forget(self.rule, who="")
        self.assertTrue(self._active(self.rule))
        self.assertEqual(len(self._denied()), 2)

    def test_non_system_cannot_modify_p1_p2_p3(self):
        entries = [
            self.s.add_knowledge("persona", layer="P1", who="system"),
            self.s.add_knowledge("raw session", layer="P2", who="gateway"),
            self.s.add_knowledge("tool wire", layer="P3", who="cron"),
        ]
        for eid in entries:
            with self.assertRaises(WriteDenied):
                self.s.update_knowledge(eid, content="hijack", who="tool:p_layer")
            with self.assertRaises(WriteDenied):
                self.s.forget(eid, who="tool:p_layer")
            self.assertTrue(self._active(eid))

        self.s.snapshot_create("v1")
        p1_after = self.s.add_knowledge("p1 after", layer="P1", who="system")
        p2_after = self.s.add_knowledge("p2 after", layer="P2", who="gateway")
        p3_after = self.s.add_knowledge("p3 after", layer="P3", who="cron")
        p5_after = self.s.add_knowledge("p5 after", layer="P5", who="tool:p_layer")
        n = self.s.snapshot_rollback("v1", who="tool:p_layer")
        self.assertEqual(n, 1)
        self.assertEqual(self.s.last_rollback_skipped, 3)
        for eid in (p1_after, p2_after, p3_after):
            self.assertTrue(self._active(eid))
        self.assertFalse(self._active(p5_after))

    def test_mcp_snapshot_create_records_caller_who(self):
        r = self._mcp("snapshot_create", {"version_id": "v1"})
        self.assertIn("created", r["result"]["content"][0]["text"])
        audit = [e for e in self.s.audit_log(limit=20) if e["action"] == "snapshot_create"][0]
        self.assertEqual(audit["who"], "tool:p_layer")

    def test_mcp_ordinary_p5_update_forget_still_work(self):
        r = self._mcp("remember", {"fact": "use portone v2", "type": "decision"})
        self.assertIn("saved", r["result"]["content"][0]["text"])
        kid = int(r["result"]["content"][0]["text"].split("#")[1].split()[0])
        r = self._mcp("update", {"id": kid, "fact": "use portone v3"})
        self.assertIn("updated", r["result"]["content"][0]["text"])
        r = self._mcp("forget", {"id": kid + 1})
        self.assertIn("superseded", r["result"]["content"][0]["text"])


class CompatMcpProtectedLayerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = KnowledgeDB(db_dir=tmp.name)
        self.addCleanup(self.db.close)
        self.rule = self.db.insert(
            layer="P0", type="decision", content="NEVER edit /protected", who="system"
        )["id"]

    def _call(self, tool, args):
        return compat_handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": tool, "arguments": args}},
            self.db,
        )

    def _active(self, entry_id):
        row = self.db._store.db.execute(
            "SELECT superseded_by FROM knowledge WHERE id=?", (entry_id,)
        ).fetchone()
        return row["superseded_by"] is None

    def test_compat_mcp_update_and_forget_p0_denied(self):
        r_upd = self._call("knowledge_update", {"id": self.rule, "fact": "edits allowed"})
        self.assertIn("error", r_upd)
        self.assertIn("does not allow", r_upd["error"]["message"])
        r_fgt = self._call("knowledge_forget", {"id": self.rule, "reason": "cleanup"})
        self.assertIn("error", r_fgt)
        self.assertTrue(self._active(self.rule))
        denied = self.db._store.audit_log(limit=20, denied_only=True)
        self.assertEqual(len(denied), 2)
        self.assertTrue(all(e["who"] == "tool:p-layers" for e in denied))

    def test_compat_mcp_snapshot_create_and_partial_rollback(self):
        self._call("knowledge_snapshot-create", {"version_id": "v1"})
        create_audit = [
            e for e in self.db._store.audit_log(limit=20) if e["action"] == "snapshot_create"
        ][0]
        self.assertEqual(create_audit["who"], "tool:p-layers")

        p0_after = self.db.insert(
            layer="P0", type="decision", content="late protected rule", who="system"
        )["id"]
        p5_after = json.loads(
            self._call("knowledge_remember", {"fact": "late tool note"})["result"]["content"][0]["text"]
        )["id"]

        r = self._call("knowledge_snapshot-rollback", {"version_id": "v1"})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertEqual(payload["rolled_back"], 1)
        self.assertEqual(payload["skipped_protected"], 1)
        self.assertTrue(self._active(p0_after))
        self.assertFalse(self._active(p5_after))


class CliProtectedLayerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = Path(tmp.name) / "memory.db"

    def _run(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli_main(["--db", str(self.db_path), "--embed", "none", *args])
        return code, buf.getvalue()

    def test_cli_who_enforces_acl_and_reports_skipped(self):
        code, out = self._run("remember", "NEVER bypass gate", "--layer", "P0")
        self.assertEqual(code, 0)
        rule_id = int(out.split("#")[1].split()[0])

        with self.assertRaises(WriteDenied):
            self._run("forget", str(rule_id), "--who", "tool:p_layer")
        with self.assertRaises(WriteDenied):
            self._run("update", str(rule_id), "--content", "bypass", "--who", "tool:p_layer")

        self._run("snapshot", "create", "v1", "--who", "tool:p_layer")
        code, out_p0 = self._run("remember", "late rule", "--layer", "P0")
        self.assertEqual(code, 0)
        p0_id = int(out_p0.split("#")[1].split()[0])
        code, out_p5 = self._run("remember", "late note", "--layer", "P5", "--who", "tool:p_layer")
        self.assertEqual(code, 0)
        p5_id = int(out_p5.split("#")[1].split()[0])

        code, out_rb = self._run("snapshot", "rollback", "v1", "--who", "tool:p_layer")
        self.assertEqual(code, 0)
        self.assertIn("1 entry(ies) superseded; 1 protected entry(ies) left unchanged", out_rb)

        s = Store(str(self.db_path), embedder=NoopEmbedder())
        self.addCleanup(s.close)
        self.assertIsNone(
            s.db.execute("SELECT superseded_by FROM knowledge WHERE id=?", (p0_id,)).fetchone()["superseded_by"]
        )
        self.assertIsNotNone(
            s.db.execute("SELECT superseded_by FROM knowledge WHERE id=?", (p5_id,)).fetchone()["superseded_by"]
        )
        sc_audit = [e for e in s.audit_log(limit=20) if e["action"] == "snapshot_create"][0]
        self.assertEqual(sc_audit["who"], "tool:p_layer")


if __name__ == "__main__":
    unittest.main()
