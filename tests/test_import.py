import sqlite3
import tempfile
import unittest
from pathlib import Path

from memcore.embed import NoopEmbedder
from memcore.import_drewgent import import_drewgent
from memcore.store import Store


def _make_drewgent_db(path: Path):
    db = sqlite3.connect(str(path))
    db.executescript(
        """
        CREATE TABLE knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL DEFAULT 'fact',
            content TEXT NOT NULL,
            source TEXT,
            created_at TEXT
        );
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            type TEXT NOT NULL,
            type_parent TEXT,
            properties TEXT DEFAULT '{}'
        );
        CREATE TABLE relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            properties TEXT DEFAULT '{}'
        );
        """
    )
    db.execute("INSERT INTO knowledge (type, content, source, created_at) VALUES (?,?,?,?)",
               ("decision", "switched to portone v2", "session: 2026-08-07 payments", "2026-08-07T09:00:00Z"))
    db.execute("INSERT INTO knowledge (type, content, source, created_at) VALUES (?,?,?,?)",
               ("pattern", "webhook retry with exponential backoff", None, "2026-08-07T09:05:00Z"))
    db.execute("INSERT INTO entities (label, type, properties) VALUES (?,?,?)",
               ("portone", "tool", "{}"))
    db.execute("INSERT INTO entities (label, type, properties) VALUES (?,?,?)",
               ("deploy-failed", "incident", "{}"))
    db.execute("INSERT INTO relations (source_id, target_id, type) VALUES (?,?,?)", (2, 1, "references"))
    db.commit()
    db.close()


class ImportTests(unittest.TestCase):
    def test_import_drewgent_db(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = Path(tmp.name) / "drewgent.db"
        _make_drewgent_db(src)

        store = Store(str(Path(tmp.name) / "memcore.db"), embedder=NoopEmbedder())
        self.addCleanup(store.close)
        summary = import_drewgent(src, store, reembed=False)

        self.assertEqual(summary["knowledge_imported"], 2)
        self.assertEqual(summary["entities_imported"], 2)
        self.assertEqual(summary["relations_imported"], 1)

        results = store.recall("portone")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source"], "session: 2026-08-07 payments")
        self.assertEqual(results[0]["created_at"], "2026-08-07T09:00:00Z")

        stats = store.stats()
        self.assertEqual(stats["knowledge"], 2)
        self.assertEqual(stats["entities"], 2)
        self.assertEqual(stats["relations"], 1)


if __name__ == "__main__":
    unittest.main()
