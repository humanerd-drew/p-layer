"""p_layer.drewdb — the transplant: p_layer-managed connection for a
drew.db-style database. Same file, same tables, managed connection."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from p_layer import __version__
from p_layer.drewdb import management_info, open_managed_connection


def _make_drew_style_db(path):
    db = sqlite3.connect(str(path))
    db.executescript(
        """
        CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, type TEXT);
        CREATE TABLE edges (id INTEGER PRIMARY KEY, src INTEGER, dst INTEGER, rel TEXT);
        CREATE TABLE memories (id INTEGER PRIMARY KEY, kind TEXT, content TEXT, confidence REAL);
        """
    )
    db.execute("INSERT INTO nodes VALUES (1,'payment','tool'),(2,'retry','pattern')")
    db.execute("INSERT INTO edges VALUES (1,1,2,'depends_on')")
    db.execute("INSERT INTO memories VALUES (1,'fact','portone v2',0.9)")
    db.commit()
    db.close()


class DrewdbTests(unittest.TestCase):
    def test_managed_connection_governs_and_preserves(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = Path(tmp.name) / "drew.db"
        _make_drew_style_db(src)

        conn = open_managed_connection(src)
        self.addCleanup(conn.close)
        # management discipline
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 30000)
        self.assertEqual(conn.row_factory, sqlite3.Row)
        # data untouched
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 1)

        # registry records the management state (additive only)
        info = management_info(src)
        self.assertEqual(info["p_layer.managed"], "true")
        self.assertIn("p_layer.schema_checksum", info)
        self.assertEqual(info["p_layer.version"], __version__)

    def test_managed_connection_is_additive(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = Path(tmp.name) / "drew.db"
        _make_drew_style_db(src)
        conn = open_managed_connection(src)
        self.addCleanup(conn.close)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("p_layer_meta", tables)
        self.assertIn("nodes", tables)
        # no foreign tables added, no data changed
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 1)

    def test_management_info_on_unmanaged_db(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = Path(tmp.name) / "plain.db"
        _make_drew_style_db(src)
        self.assertEqual(management_info(src), {"managed": "false"})

    def test_missing_vec0_degrades_gracefully(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = Path(tmp.name) / "drew.db"
        _make_drew_style_db(src)
        conn = open_managed_connection(src, vec0_path=Path(tmp.name) / "nope.dylib")
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
