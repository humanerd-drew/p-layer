"""p_layer.drewdb — the transplant: p_layer-managed connection for drewgent's
live memory database (drew.db).

The `agentmemory` MCP server (drew_db_server.py) keeps its exact schema, its
memory_* tools, its injection timing, and its data. Only the db management
changes: the connection is opened and governed by p_layer — same file, same
tables, with WAL, a busy timeout, and a management registry that records
when, with which p_layer version, and with which schema checksum the
database is managed.

This is the transparent transplant (Option A): engine swapped, interface
untouched. The server's `connect()` delegates here; if p_layer is ever
missing, the server falls back to its original connection code, so the
interface can never be broken by this layer.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["open_managed_connection", "management_info"]

MANAGED_KEY = "p_layer.managed"
VERSION_KEY = "p_layer.version"
SCHEMA_KEY = "p_layer.schema_checksum"

_REGISTRY_LOCK = threading.Lock()
_REGISTERED_DBS: set[tuple[str, int, int, str]] = set()


def _reset_registry_cache() -> None:
    """Clear the per-process registration cache (primarily for tests)."""
    with _REGISTRY_LOCK:
        _REGISTERED_DBS.clear()


def _current_version() -> str:
    try:
        import p_layer  # lazy: avoids a circular import at package load

        return str(p_layer.__version__)
    except Exception:
        return "unknown"


def _db_identity(db_path: str | Path, version: str) -> tuple[str, int, int, str] | None:
    try:
        resolved = Path(db_path).resolve()
        st = os.stat(resolved)
    except OSError:
        return None
    return (str(resolved), st.st_dev, st.st_ino, version)


def _schema_checksum(conn: sqlite3.Connection) -> str:
    h = hashlib.sha256()
    for (sql,) in conn.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
    ).fetchall():
        h.update(sql.encode("utf-8"))
    return h.hexdigest()[:16]


def _register_management(conn: sqlite3.Connection, version: str | None = None) -> None:
    """Record management state in an additive p_layer_meta table.

    Never touches existing tables; creates its own registry. The schema
    checksum lets a future migration verify the db hasn't drifted from what
    p_layer last managed.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS p_layer_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    now = datetime.now(timezone.utc).isoformat()
    upsert = (
        "INSERT INTO p_layer_meta (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at"
    )
    conn.execute(upsert, (MANAGED_KEY, "true", now))
    conn.execute(upsert, (SCHEMA_KEY, _schema_checksum(conn), now))
    conn.execute(upsert, (VERSION_KEY, _current_version() if version is None else version, now))
    conn.commit()


def open_managed_connection(
    db_path: str | Path,
    vec0_path: str | Path | None = None,
    timeout: int = 30,
    *,
    register: bool | None = None,
) -> sqlite3.Connection:
    """Open a drew.db-style database the p_layer way.

    Same file, managed connection:
    - row_factory = sqlite3.Row (what drew_db_server expects)
    - busy_timeout 30s — writes never silently fail under contention
    - journal_mode = WAL (checked read-first; only switched when not WAL)
    - sqlite-vec loaded when vec0_path is provided (same graceful degradation)
    - p_layer_meta registry records management state (once per process & db
      inode/version by default; pass register=True to force-refresh or
      register=False to keep pure read connections lock-free)

    foreign_keys is deliberately NOT force-enabled here: flipping it on a
    legacy database whose rows may predate FK constraints can break live
    writes. Integrity validation is a separate, data-checked step.
    """
    conn = sqlite3.connect(str(db_path), timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    mode_row = conn.execute("PRAGMA journal_mode").fetchone()
    if not mode_row or str(mode_row[0]).lower() != "wal":
        conn.execute("PRAGMA journal_mode=WAL")
    if vec0_path:
        conn.enable_load_extension(True)
        try:
            conn.load_extension(str(vec0_path))
        except sqlite3.OperationalError:
            pass  # vec0 미로드 — 벡터 검색 불가 시 BM25만
    if register is not False:
        version = _current_version()
        ident = _db_identity(db_path, version)
        if register is True:
            _register_management(conn, version=version)
            if ident is not None:
                with _REGISTRY_LOCK:
                    _REGISTERED_DBS.add(ident)
        else:
            with _REGISTRY_LOCK:
                already_registered = ident is not None and ident in _REGISTERED_DBS
            if not already_registered:
                _register_management(conn, version=version)
                if ident is not None:
                    with _REGISTRY_LOCK:
                        _REGISTERED_DBS.add(ident)
    return conn


def management_info(db_path: str | Path) -> dict:
    """Read the management registry (read-only)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT key, value FROM p_layer_meta").fetchall()
        return {k: v for k, v in rows}
    except sqlite3.OperationalError:
        return {"managed": "false"}
    finally:
        conn.close()
