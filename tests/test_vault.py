"""Vault ingest tests: rules.md -> rules table, incidents dir -> episodes."""
import tempfile
import unittest
from pathlib import Path

from p_layer.embed import NoopEmbedder
from p_layer.store import Store


def _store(self):
    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)
    s = Store(str(Path(tmp.name) / "memory.db"), embedder=NoopEmbedder())
    self.addCleanup(s.close)
    return s, tmp.name


class RulesMdImportTests(unittest.TestCase):
    def test_import_rules_md(self):
        s, tmp = _store(self)
        rules = Path(tmp) / "rules.md"
        rules.write_text(
            "# Brain Rules\n"
            "\n"
            "## [P0] never expose secrets in logs\n"
            "priority: 5\n"
            "scope: all\n"
            "\n"
            "## [P1] be concise in responses\n"
            "\n"
            "## deployment policy\n"
            "priority: 200\n"
            "condition: deploy\n"
        )
        n = s.import_rules_md(rules)
        self.assertEqual(n, 3)
        rules_list = s.enabled_rules()
        by_text = {r["text"]: r for r in rules_list}
        self.assertEqual(by_text["never expose secrets in logs"]["priority"], 5)
        self.assertEqual(by_text["never expose secrets in logs"]["layer"], "P0")
        self.assertEqual(by_text["be concise in responses"]["layer"], "P1")
        # no [Px] heading -> defaults to P0
        self.assertEqual(by_text["deployment policy"]["layer"], "P0")
        self.assertEqual(by_text["deployment policy"]["condition"], "deploy")

    def test_import_rules_md_idempotent(self):
        s, tmp = _store(self)
        rules = Path(tmp) / "rules.md"
        rules.write_text("## [P0] never expose secrets\n")
        self.assertEqual(s.import_rules_md(rules), 1)
        self.assertEqual(s.import_rules_md(rules), 0)  # dedupe
        self.assertEqual(len(s.enabled_rules()), 1)


class IncidentsImportTests(unittest.TestCase):
    def test_import_incidents_dir(self):
        s, tmp = _store(self)
        incidents = Path(tmp) / "incidents"
        incidents.mkdir()
        (incidents / "2026-08-01-deploy.md").write_text("# deploy failed\nroot cause: missing env\n")
        (incidents / "2026-08-02-quota.md").write_text("# quota exceeded\n")
        n = s.import_incidents_dir(incidents)
        self.assertEqual(n, 2)
        episodes = s.db.execute(
            "SELECT payload FROM episodes WHERE kind='incident' ORDER BY payload"
        ).fetchall()
        self.assertEqual(len(episodes), 2)
        self.assertIn("2026-08-01-deploy.md", episodes[0]["payload"])

    def test_import_incidents_dir_idempotent(self):
        s, tmp = _store(self)
        incidents = Path(tmp) / "incidents"
        incidents.mkdir()
        (incidents / "a.md").write_text("# a\n")
        self.assertEqual(s.import_incidents_dir(incidents), 1)
        self.assertEqual(s.import_incidents_dir(incidents), 0)  # dedupe per file
        self.assertEqual(s.stats()["episodes"], 1)


if __name__ == "__main__":
    unittest.main()
