import json
import tempfile
import unittest
from pathlib import Path

from memcore.embed import HashEmbedder
from memcore.mcp import handle_message
from memcore.store import Store


class McpTests(unittest.TestCase):
    def _store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(str(Path(tmp.name) / "memory.db"), embedder=HashEmbedder())
        self.addCleanup(store.close)
        return store

    def test_initialize(self):
        resp = handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, self._store())
        self.assertEqual(resp["id"], 1)
        self.assertIn("protocolVersion", resp["result"])
        self.assertIn("tools", resp["result"]["capabilities"])

    def test_notification_no_response(self):
        resp = handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, self._store())
        self.assertIsNone(resp)

    def test_tools_list(self):
        resp = handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, self._store())
        names = {t["name"] for t in resp["result"]["tools"]}
        self.assertEqual(
            names,
            {"remember", "recall", "forget", "update",
             "snapshot_create", "snapshot_rollback", "memory_stats", "assemble",
             "memory_audit", "graph_explore", "graph_trace", "graph_rca",
             "consolidate"},
        )

    def test_remember_then_recall(self):
        s = self._store()
        r1 = handle_message(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "remember", "arguments": {"fact": "use portone v2", "type": "decision"}}},
            s,
        )
        self.assertIn("saved", r1["result"]["content"][0]["text"])
        r2 = handle_message(
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "recall", "arguments": {"query": "portone"}}},
            s,
        )
        self.assertIn("portone", r2["result"]["content"][0]["text"])

    def test_unknown_tool_error(self):
        resp = handle_message(
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "nope", "arguments": {}}},
            self._store(),
        )
        self.assertIn("error", resp)

    def test_json_roundtrip_through_serve_streams(self):
        import io

        s = self._store()
        stdin = io.StringIO(
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
            '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"remember","arguments":{"fact":"hello world"}}}\n'
        )
        stdout = io.StringIO()
        from memcore.mcp import serve

        serve(s, stdin=stdin, stdout=stdout)
        lines = [json.loads(l) for l in stdout.getvalue().strip().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["result"]["serverInfo"]["name"], "memcore")
        self.assertIn("saved", lines[1]["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
