"""Tests for the read-only, zero-dependency 2D/3D p_layer.ui visualizer."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

from p_layer.embed import HashEmbedder
from p_layer.store import Store, WriteDenied
from p_layer.ui import PLayerGraphAdapter, PLayerUIServer, connect_readonly, is_loopback_host, make_handler, serve_ui


class TestPLayerUI(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "memory ui.db"
        self.store = Store(self.db_path, embedder=HashEmbedder())
        self.addCleanup(self.store.close)

        self.k_rule = self.store.add_knowledge(
            "Never commit API secrets",
            type="decision",
            layer="P0",
            who="system",
            source="constitution",
        )
        self.k_pay = self.store.add_knowledge(
            "결제 수단은 PortOne v2로 통일한다",
            type="decision",
            layer="P5",
            who="agent",
            source="onboarding",
        )
        self.k_obs = self.store.add_knowledge(
            "Onboarding billing webhook verified",
            type="fact",
            layer="P5",
            who="agent",
            source="onboarding",
        )
        e_srv = self.store.add_entity("billing-service", "tool", knowledge_id=self.k_pay)
        e_pat = self.store.add_entity("webhook-retry", "pattern")
        self.store.add_relation(e_srv, e_pat, "relates_to")
        self.store.add_rule("Always audit P0 writes", layer="P0", priority=10)
        self.store.record_episode("session", "Webhook smoke check passed", session_id="s1")

        # Trigger one denied write so audit_log has both allowed and denied entries
        with self.assertRaises(WriteDenied):
            self.store.add_knowledge("override P0", layer="P0", who="agent")

    def test_connect_readonly_blocks_writes(self) -> None:
        conn = connect_readonly(self.db_path)
        try:
            cnt = conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
            self.assertEqual(cnt, 3)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM knowledge")
        finally:
            conn.close()

    def test_adapter_curated_ontology_all_and_korean_search(self) -> None:
        adapter = PLayerGraphAdapter(self.db_path)
        g = adapter.build_graph(mode="curated")
        ids = {n["id"] for n in g["nodes"]}
        self.assertIn(f"k:{self.k_rule}", ids)
        self.assertIn(f"k:{self.k_pay}", ids)
        self.assertTrue(any(n["group"] == "entity" for n in g["nodes"]))
        self.assertTrue(any(n["group"] == "rule" for n in g["nodes"]))
        self.assertGreaterEqual(g["stats"]["denied_writes"], 1)
        self.assertTrue(any(a["denied"] for a in g["audit"]))

        # Shared source links onboarding entries cleanly
        edge_rels = {e["rel"] for e in g["edges"]}
        self.assertIn("same_source", edge_rels)
        self.assertIn("grounded_in", edge_rels)

        # Korean substring search parity
        kr = adapter.build_graph(query="결제")
        self.assertIn(f"k:{self.k_pay}", {n["id"] for n in kr["nodes"]})

        # Ontology & All modes
        onto = adapter.build_graph(mode="ontology")
        self.assertTrue(all(n["group"] in ("entity", "rule") for n in onto["nodes"]))
        all_g = adapter.build_graph(mode="all")
        self.assertTrue(any(n["group"] == "episode" for n in all_g["nodes"]))

    def test_node_detail_and_live_sse_diff(self) -> None:
        adapter = PLayerGraphAdapter(self.db_path)
        detail = adapter.get_node_detail(f"k:{self.k_pay}")
        self.assertIn("PortOne v2", detail["content"])
        self.assertEqual(detail["metadata"]["layer"], "P5")
        self.assertTrue(any(nb["rel"] == "grounded_entity" for nb in detail["neighbors"]))

        prev = adapter.poll_cursor()
        k_new = self.store.add_knowledge("Live SSE memory item", layer="P5", who="agent")
        curr = adapter.poll_cursor()
        events = adapter.diff_events(prev, curr)
        self.assertTrue(any(ev["type"] == "memory_added" and ev["node_id"] == f"k:{k_new}" for ev in events))
        self.assertTrue(any(ev["type"] == "audit_event" for ev in events))

    def test_loopback_guard_and_http_endpoints(self) -> None:
        self.assertTrue(is_loopback_host("127.0.0.1"))
        self.assertFalse(is_loopback_host("0.0.0.0"))
        with self.assertRaises(ValueError):
            serve_ui(self.db_path, host="0.0.0.0", port=0, open_browser=False)

        srv = PLayerUIServer(("127.0.0.1", 0), make_handler(self.db_path))
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        port = srv.server_address[1]

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as r:
            self.assertTrue(json.loads(r.read())["ok"])
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as r:
            self.assertIn("p-layer Memory Graph", r.read().decode("utf-8"))
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/graph?q={urllib.parse.quote('결제')}", timeout=3) as r:
            g = json.loads(r.read())
            self.assertIn(f"k:{self.k_pay}", {n["id"] for n in g["nodes"]})
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/node/{urllib.parse.quote(f'k:{self.k_pay}')}", timeout=3) as r:
            d = json.loads(r.read())
            self.assertEqual(d["metadata"]["layer"], "P5")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/events?once=1", timeout=3) as r:
            self.assertIn("event: connected", r.read().decode("utf-8"))

    def test_cli_ui_help_and_non_loopback_rejection(self) -> None:
        res = subprocess.run(
            [sys.executable, "-m", "p_layer", "ui", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("--no-open", res.stdout)

        bad = subprocess.run(
            [sys.executable, "-m", "p_layer", "--db", str(self.db_path), "ui", "--host", "0.0.0.0", "--no-open"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(bad.returncode, 0)


if __name__ == "__main__":
    unittest.main()
