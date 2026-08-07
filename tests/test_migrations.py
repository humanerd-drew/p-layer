import sqlite3
import tempfile
import unittest
from pathlib import Path

from memcore.migrations import MIGRATIONS, _checksum, applied_versions, migrate


class MigrationTests(unittest.TestCase):
    def _fresh(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = sqlite3.connect(str(Path(tmp.name) / "m.db"))
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        return db

    def test_fresh_migrate_creates_schema(self):
        db = self._fresh()
        migrate(db)
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("knowledge", tables)
        self.assertIn("knowledge_fts", tables)
        self.assertIn("embeddings", tables)
        self.assertIn("episodes", tables)
        self.assertIn("entities", tables)
        self.assertIn("relations", tables)
        self.assertIn("rules", tables)
        self.assertIn("schema_migrations", tables)
        self.assertEqual(applied_versions(db), {v for v, _, _ in MIGRATIONS})

    def test_migrate_is_idempotent(self):
        db = self._fresh()
        migrate(db)
        migrate(db)  # no error, no duplicate rows
        self.assertEqual(applied_versions(db), {v for v, _, _ in MIGRATIONS})

    def test_checksum_tamper_detected(self):
        db = self._fresh()
        migrate(db)
        db.execute("UPDATE schema_migrations SET checksum = 'tampered'")
        db.commit()
        with self.assertRaises(RuntimeError):
            migrate(db)

    def test_forward_only_duplicate_version_rejected(self):
        from memcore import migrations as m

        with self.assertRaises(RuntimeError):
            m._register(1, "duplicate", "SELECT 1")

    def test_checksum_stable(self):
        # The applied checksum must equal a fresh checksum of the same SQL.
        version, name, sql = MIGRATIONS[0]
        db = self._fresh()
        migrate(db)
        stored = db.execute(
            "SELECT checksum FROM schema_migrations WHERE version = ?", (version,)
        ).fetchone()[0]
        self.assertEqual(stored, _checksum(sql))


if __name__ == "__main__":
    unittest.main()
