"""Wire-level real-client test: the memcore MCP server driven through real
OS stdio pipes with the exact protocol message sequence a client performs
(initialize -> notifications/initialized -> tools/list -> tools/call).

No SDK dependency — this is the transport layer any MCP client uses, verified
end to end. (The official Python SDK's stdio client is broken on macOS in this
environment; see test_mcp_sdk_client.py, which runs in CI on ubuntu.)
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def _spawn_server(store_path: str):
    env = dict(os.environ, MEMCORE_DB=store_path, MEMCORE_EMBED="hash")
    return subprocess.Popen(
        [sys.executable, "-m", "memcore", "serve"],
        env=env,
        cwd=Path(__file__).resolve().parent.parent,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


class WireClient:
    """A minimal client that speaks the MCP stdio protocol over pipes."""

    def __init__(self, proc):
        self.proc = proc
        self._next_id = 0

    def send(self, method, params=None):
        self._next_id += 1
        msg = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def notify(self, method):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()


class WireMcpTests(unittest.TestCase):
    def _session(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store_path = str(Path(tmp.name) / "memory.db")
        proc = _spawn_server(store_path)

        def _cleanup():
            if proc.poll() is None:
                proc.kill()
            for f in (proc.stdin, proc.stdout, proc.stderr):
                if f is not None:
                    f.close()

        self.addCleanup(_cleanup)
        return WireClient(proc), store_path

    def test_full_handshake_and_tool_roundtrip(self):
        client, _ = self._session()
        # initialize
        resp = client.send("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                          "clientInfo": {"name": "wire", "version": "1"}})
        self.assertEqual(resp["result"]["serverInfo"]["name"], "memcore")
        client.notify("notifications/initialized")

        # tools/list
        resp = client.send("tools/list")
        names = {t["name"] for t in resp["result"]["tools"]}
        self.assertIn("remember", names)
        self.assertIn("graph_rca", names)
        self.assertIn("memory_audit", names)

        # tools/call: remember -> recall -> stats
        resp = client.send("tools/call", {"name": "remember",
                                          "arguments": {"fact": "switched to portone v2", "type": "decision"}})
        self.assertIn("saved #1", resp["result"]["content"][0]["text"])

        resp = client.send("tools/call", {"name": "recall", "arguments": {"query": "portone", "limit": 3}})
        self.assertIn("portone v2", resp["result"]["content"][0]["text"])

        resp = client.send("tools/call", {"name": "memory_stats", "arguments": {}})
        self.assertIn("by_layer", resp["result"]["content"][0]["text"])

        # ping keeps the session alive
        resp = client.send("ping")
        self.assertEqual(resp["result"], {})

    def test_denied_write_surfaces_as_tool_error(self):
        client, _ = self._session()
        client.send("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                   "clientInfo": {"name": "wire", "version": "1"}})
        client.notify("notifications/initialized")
        resp = client.send("tools/call", {"name": "remember", "arguments": {"fact": "x", "layer": "P0"}})
        self.assertIn("error", resp)
        self.assertIn("does not allow", resp["error"]["message"])

    def test_unknown_tool_is_protocol_error(self):
        client, _ = self._session()
        client.send("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                   "clientInfo": {"name": "wire", "version": "1"}})
        client.notify("notifications/initialized")
        resp = client.send("tools/call", {"name": "nope", "arguments": {}})
        self.assertEqual(resp["error"]["code"], -32000)

    def test_server_stays_alive_across_calls(self):
        client, _ = self._session()
        client.send("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                   "clientInfo": {"name": "wire", "version": "1"}})
        for i in range(5):
            resp = client.send("tools/call", {"name": "memory_stats", "arguments": {}})
            self.assertIn("result", resp)
        self.assertIsNone(client.proc.poll(), "server process must stay alive")


if __name__ == "__main__":
    unittest.main()
