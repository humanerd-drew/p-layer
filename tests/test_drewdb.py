"""p_layer.drewdb — the transplant: p_layer-managed connection for a
drew.db-style database. Same file, same tables, managed connection."""
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from p_layer import __version__
from p_layer.drewdb import (
    _reset_registry_cache,
    management_info,
    open_managed_connection,
)


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
    def setUp(self):
        _reset_registry_cache()
        self.addCleanup(_reset_registry_cache)

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

    def test_repeated_open_skips_writes_and_reads_under_write_lock(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = Path(tmp.name) / "drew.db"
        _make_drew_style_db(src)

        conn1 = open_managed_connection(src)
        first_updated_at = conn1.execute(
            "SELECT updated_at FROM p_layer_meta WHERE key='p_layer.managed'"
        ).fetchone()[0]
        first_data_version = conn1.execute("PRAGMA data_version").fetchone()[0]
        conn1.close()

        writer = sqlite3.connect(str(src), isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")
        try:
            t0 = time.monotonic()
            conn2 = open_managed_connection(src, timeout=1)
            self.addCleanup(conn2.close)
            count = conn2.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            elapsed = time.monotonic() - t0
            second_updated_at = conn2.execute(
                "SELECT updated_at FROM p_layer_meta WHERE key='p_layer.managed'"
            ).fetchone()[0]
            second_data_version = conn2.execute("PRAGMA data_version").fetchone()[0]
        finally:
            writer.execute("ROLLBACK")

        self.assertEqual(count, 1)
        self.assertLess(elapsed, 1.0)
        self.assertEqual(second_updated_at, first_updated_at)
        self.assertEqual(second_data_version, first_data_version)

    def test_register_false_and_register_true_controls(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = Path(tmp.name) / "drew.db"
        _make_drew_style_db(src)

        conn_ro = open_managed_connection(src, register=False)
        self.assertEqual(conn_ro.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(conn_ro.execute("PRAGMA busy_timeout").fetchone()[0], 30000)
        self.assertEqual(conn_ro.row_factory, sqlite3.Row)
        conn_ro.close()
        self.assertEqual(management_info(src), {"managed": "false"})

        conn1 = open_managed_connection(src)
        c1 = conn1.execute(
            "SELECT value FROM p_layer_meta WHERE key='p_layer.schema_checksum'"
        ).fetchone()[0]
        conn1.execute("CREATE TABLE extra (id INTEGER PRIMARY KEY)")
        conn1.commit()
        conn1.close()

        # Default subsequent open skips re-registration
        conn2 = open_managed_connection(src)
        c2_cached = conn2.execute(
            "SELECT value FROM p_layer_meta WHERE key='p_layer.schema_checksum'"
        ).fetchone()[0]
        conn2.close()
        self.assertEqual(c2_cached, c1)

        # register=True forces checksum refresh
        conn3 = open_managed_connection(src, register=True)
        self.addCleanup(conn3.close)
        c3 = conn3.execute(
            "SELECT value FROM p_layer_meta WHERE key='p_layer.schema_checksum'"
        ).fetchone()[0]
        self.assertNotEqual(c3, c1)

    def test_recreated_db_at_same_path_reregisters(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = Path(tmp.name) / "drew.db"
        _make_drew_style_db(src)

        conn1 = open_managed_connection(src)
        conn1.close()

        # Keep an open fd on the unlinked file so the OS assigns a new inode to the recreated DB.
        held_fd = open(src, "rb")
        try:
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(src) + suffix)
                if p.exists():
                    p.unlink()
            _make_drew_style_db(src)
        finally:
            held_fd.close()

        self.assertEqual(management_info(src), {"managed": "false"})
        conn2 = open_managed_connection(src)
        self.addCleanup(conn2.close)
        info = management_info(src)
        self.assertEqual(info["p_layer.managed"], "true")
        self.assertEqual(info["p_layer.version"], __version__)


if __name__ == "__main__":
    unittest.main()
