"""P0 ontology gate + drift report tests."""
import json
import tempfile
import unittest
from pathlib import Path

from p_layer import gate
from p_layer.embed import NoopEmbedder
from p_layer.store import Store


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.onto = Path(self.tmp.name) / "p0-brain-ontology.jsonl"
        self.props = Path(self.tmp.name) / "rule_proposals"
        self.onto.write_text(
            '{"id":"r1","type":"policy","space":"policy","title":"규칙1","file":"r1.md","links":[]}\n',
            encoding="utf-8",
        )

    def _gate(self, **kw):
        kw.setdefault("ontology_path", self.onto)
        kw.setdefault("proposals_dir", self.props)
        return kw

    def test_validate_ok(self):
        r = gate.validate(self.onto)
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 1)

    def test_bad_json_line_rejected(self):
        self.onto.write_text("{broken\n", encoding="utf-8")
        r = gate.validate(self.onto)
        self.assertFalse(r["ok"])
        self.assertTrue(any("parse" in e for e in r["errors"]))

    def test_duplicate_id_rejected(self):
        self.onto.write_text(
            '{"id":"r1","type":"policy","space":"s","title":"t","file":"f","links":[]}\n'
            '{"id":"r1","type":"policy","space":"s","title":"t2","file":"f2","links":[]}\n',
            encoding="utf-8",
        )
        self.assertFalse(gate.validate(self.onto)["ok"])

    def test_apply_requires_approval(self):
        r = gate.propose("r9", "policy", "s", "t", "f", "lesson:x", proposals_dir=self.props)
        self.assertTrue(r["ok"])
        self.assertFalse(gate.apply("r9", **self._gate())["ok"])  # proposed -> rejected
        gate.approve("r9", proposals_dir=self.props)
        self.assertTrue(gate.apply("r9", **self._gate())["ok"])
        # idempotent: re-apply no-op, no duplicate line
        gate.apply("r9", **self._gate())
        self.assertEqual(gate.validate(self.onto)["count"], 2)
        gate.deprecate("r9", proposals_dir=self.props)
        self.assertEqual(gate._load_proposal("r9", self.props)["status"], "deprecated")

    def test_duplicate_id_apply_blocked(self):
        gate.propose("r1", "policy", "s", "t", "f", "lesson:x", proposals_dir=self.props)
        gate.approve("r1", proposals_dir=self.props)
        r = gate.apply("r1", **self._gate())
        self.assertTrue(r["ok"])  # no-op (id exists), no crash
        self.assertEqual(gate.validate(self.onto)["count"], 1)

    def test_path_traversal_id_rejected(self):
        r = gate.propose("../../etc/x", "policy", "s", "t", "f", "src", proposals_dir=self.props)
        self.assertFalse(r["ok"])

    def test_retire_requires_approval_and_removes(self):
        # retire 는 승인 없이 불가 (사람 게이트)
        gate.propose("dup-retire", "policy", "s", "t", "f", "dup-of-r1", target="r1",
                     proposals_dir=self.props)
        self.assertFalse(gate.retire("dup-retire", **self._gate())["ok"])  # proposed → 거부
        gate.approve("dup-retire", proposals_dir=self.props)
        r = gate.retire("dup-retire", **self._gate())
        self.assertTrue(r["ok"])
        self.assertEqual(gate.validate(self.onto)["count"], 0)  # r1 제거됨
        # 멱등: 재-retire no-op
        self.assertTrue(gate.retire("dup-retire", **self._gate())["ok"])

    def test_fresh(self):
        r = gate.fresh(self.onto, self.props)
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["entries"], 1)
        self.assertEqual(r["open_proposals"], 0)


class DriftReportTests(unittest.TestCase):
    def _store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Store(str(Path(tmp.name) / "memory.db"), embedder=NoopEmbedder())

    def test_first_run_establishes_baseline(self):
        s = self._store()
        s.add_knowledge("f1", type="fact", layer="P5", who="system")
        base_dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: base_dir and base_dir.__class__ and None)
        r = s.drift_report(baseline_dir=base_dir)
        self.assertEqual(r["status"], "no change")
        self.assertTrue((base_dir / "baseline.json").exists())

    def test_second_run_no_change(self):
        s = self._store()
        base_dir = Path(tempfile.mkdtemp())
        s.drift_report(baseline_dir=base_dir)
        r = s.drift_report(baseline_dir=base_dir)
        self.assertEqual(r["status"], "no change")

    def test_report_is_readonly(self):
        s = self._store()
        base_dir = Path(tempfile.mkdtemp())
        s.drift_report(baseline_dir=base_dir)
        before = s.db.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
        s.drift_report(baseline_dir=base_dir)
        after = s.db.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
