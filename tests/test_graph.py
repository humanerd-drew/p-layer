"""Graph & inference tests: explore, trace, rca, transitive closure, cycle safety."""
import tempfile
import unittest
from pathlib import Path

from p_layer.embed import NoopEmbedder
from p_layer.store import Store


def _graph_store(self):
    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)
    s = Store(str(Path(tmp.name) / "memory.db"), embedder=NoopEmbedder())
    self.addCleanup(s.close)
    portone = s.add_entity("portone", "tool")
    retry = s.add_entity("retry-policy", "pattern")
    incident = s.add_entity("deploy-failed", "incident")
    script = s.add_entity("deploy.sh", "script")
    s.add_relation(incident, retry, "fixed_by")        # incident -> pattern
    s.add_relation(retry, portone, "depends_on")       # pattern -> tool
    s.add_relation(retry, script, "depends_on")        # pattern -> script
    s.add_relation(retry, incident, "caused")          # pattern -> incident
    s.add_relation(portone, incident, "references")    # any -> any
    return s, {"portone": portone, "retry": retry, "incident": incident, "script": script}


class GraphExploreTests(unittest.TestCase):
    def test_explore_outbound_neighbors(self):
        s, ids = _graph_store(self)
        result = s.graph_explore("retry-policy", depth=2)
        self.assertEqual(len(result["entities"]), 1)
        node = result["entities"][0]
        self.assertEqual(node["label"], "retry-policy")
        rels = {(n["relation"], n["label"]) for n in node["neighbors"]}
        self.assertIn(("depends_on", "portone"), rels)
        self.assertIn(("depends_on", "deploy.sh"), rels)
        self.assertIn(("caused", "deploy-failed"), rels)

    def test_explore_fuzzy_and_miss(self):
        s, _ = _graph_store(self)
        self.assertEqual(len(s.graph_explore("porto", depth=1)["entities"]), 1)
        self.assertEqual(s.graph_explore("no-such-entity")["entities"], [])


class GraphTraceTests(unittest.TestCase):
    def test_trace_bidirectional(self):
        s, ids = _graph_store(self)
        result = s.graph_trace("deploy-failed")
        self.assertEqual(len(result["trace"]), 1)
        t = result["trace"][0]
        in_labels = {(r["relation"], r["label"]) for r in t["inbound"]}
        out_labels = {(r["relation"], r["label"]) for r in t["outbound"]}
        self.assertIn(("caused", "retry-policy"), in_labels)
        self.assertIn(("references", "portone"), in_labels)
        self.assertIn(("fixed_by", "retry-policy"), out_labels)


class GraphRcaTests(unittest.TestCase):
    def test_rca_timeline(self):
        s, _ = _graph_store(self)
        result = s.graph_rca("deploy-failed")
        self.assertEqual(len(result["root_causes"]), 1)
        rca = result["root_causes"][0]
        self.assertEqual(rca["incident"]["label"], "deploy-failed")
        kinds = [(t["type"], t["entity"]) for t in rca["timeline"]]
        self.assertIn(("cause", "retry-policy"), kinds)
        self.assertIn(("fix", "retry-policy"), kinds)

    def test_rca_skips_non_incident(self):
        s, _ = _graph_store(self)
        self.assertEqual(s.graph_rca("portone")["root_causes"], [])


class InferenceTests(unittest.TestCase):
    def test_transitive_closure_depends_on(self):
        s, ids = _graph_store(self)
        chain = s.transitive_closure(ids["retry"], "depends_on", "out", depth=3)
        labels = {r["label"] for r in chain}
        self.assertIn("portone", labels)
        self.assertIn("deploy.sh", labels)

    def test_cycle_terminates(self):
        s = _graph_store(self)[0]
        a = s.add_entity("node-a", "tool")
        b = s.add_entity("node-b", "tool")
        s.add_relation(a, b, "references")
        s.add_relation(b, a, "references")  # cycle
        out = s._traverse(a, "out", depth=6)
        labels = {r["label"] for r in out}
        self.assertIn("node-b", labels)
        self.assertLessEqual(len(out), 6)  # bounded by depth, no infinite loop

    def test_invalid_direction(self):
        s = _graph_store(self)[0]
        with self.assertRaises(ValueError):
            s._traverse(1, "sideways")


if __name__ == "__main__":
    unittest.main()
