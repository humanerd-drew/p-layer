"""
KnowledgeDB — Unified PgVector + SQLite fallback access.

Single entry point for all database operations.
Layer-aware: P0 (rules) through P6 (incidents).

Usage:
    db = KnowledgeDB()
    db.insert(layer='P5', type='knowledge', content='...')
    results = db.search('query string', layers=['P5', 'P6'])
"""

import hashlib
import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import numpy as np
    import psycopg2
    from pgvector.psycopg2 import register_vector
    HAS_PG = True
except ImportError:
    np = None
    psycopg2 = None
    register_vector = None
    HAS_PG = False

logger = logging.getLogger(__name__)

DEFAULT_DB_DIR = os.environ.get("KNOWLEDGE_DB_DIR", str(Path.cwd() / ".knowledge"))
PG_DSN = os.environ.get("KNOWLEDGE_PG_DSN", "")

LAYER_AUTHORITY = {
    "P0": 100, "P1": 80, "P2": 60, "P3": 50, "P4": 40, "P5": 30, "P6": 20,
}

LAYER_WRITERS = {
    "P0": frozenset({"system"}),
    "P1": frozenset({"system"}),
    "P2": frozenset({"system", "gateway", "cron"}),
    "P3": frozenset({"system", "gateway", "cron"}),
    "P4": frozenset({"system", "cron", "agent", "manual"}),
    "P5": frozenset({"system", "cron", "agent", "manual", "tool"}),
    "P6": frozenset({"system", "cron", "agent", "manual", "tool"}),
}


class DatabaseError(Exception):
    pass


class WriteDenied(DatabaseError):
    pass


class ConnectionError(DatabaseError):
    pass


class KnowledgeDB:
    """Unified database access with Pg primary + SQLite fallback."""

    def __init__(self, mode: str = "auto", dsn: str = None,
                 db_dir: str = None):
        self.mode = mode
        self.dsn = dsn or PG_DSN
        self.db_dir = Path(db_dir or DEFAULT_DB_DIR)
        self._pg_conn = None
        self._sqlite_conn = None
        if HAS_PG:
            self._connect_pg()

    def _connect_pg(self):
        if self.mode == "sqlite" or not self.dsn:
            return
        try:
            conn = psycopg2.connect(self.dsn)
            conn.set_session(autocommit=True)
            register_vector(conn)
            self._pg_conn = conn
        except Exception as e:
            if self.mode == "pg":
                raise ConnectionError(f"Pg connection failed: {e}")
            logger.warning("Pg unavailable, using SQLite fallback: %s", e)
            self.mode = "sqlite"

    def _connect_sqlite(self):
        if self._sqlite_conn is None:
            path = self.db_dir / "knowledge.db"
            path.parent.mkdir(parents=True, exist_ok=True)
            self._sqlite_conn = sqlite3.connect(str(path))
            self._sqlite_conn.row_factory = sqlite3.Row
            self._sqlite_conn.executescript("""
                CREATE TABLE IF NOT EXISTS memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fact TEXT NOT NULL,
                    type TEXT NOT NULL DEFAULT 'fact',
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    fact, type,
                    tokenize='porter unicode61',
                    content='memory',
                    content_rowid='id'
                );
                CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
                    INSERT INTO memory_fts(rowid, fact, type) VALUES (new.id, new.fact, new.type);
                END;
            """)
        return self._sqlite_conn

    def set_mode(self, mode: str):
        if mode not in ("auto", "pg", "sqlite"):
            raise ValueError(f"Invalid mode: {mode}")
        self.mode = mode
        if mode == "pg" and self._pg_conn is None:
            self._connect_pg()
        if mode == "sqlite" and self._sqlite_conn is None:
            self._connect_sqlite()

    @property
    def available(self) -> bool:
        if self.mode == "sqlite":
            return True
        if self._pg_conn is None:
            return False
        try:
            cur = self._pg_conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
            return True
        except Exception:
            return False

    def _check_layer_write(self, layer: str, who: str):
        allowed = LAYER_WRITERS.get(layer, frozenset())
        who_prefix = who.split(":")[0] if ":" in who else who
        if who_prefix not in allowed and who not in allowed:
            raise WriteDenied(
                f"Layer {layer} does not allow writes from '{who}'. "
                f"Allowed: {sorted(allowed)}"
            )

    def insert(self, layer: str, type: str, content: str,
               who: str = "system", source: str = "",
               source_path: str = "", embedding: list = None,
               created_at: str = None) -> dict:
        if layer not in LAYER_AUTHORITY:
            raise ValueError(f"Invalid layer: {layer}. Must be P0-P6")
        self._check_layer_write(layer, who)

        data = {
            "who": who, "type": type, "layer": layer,
            "authority": LAYER_AUTHORITY[layer],
            "content": content[:100000],
            "source": source, "source_path": source_path,
        }
        if created_at:
            data["created_at"] = created_at

        if self.mode != "sqlite" and self._pg_conn:
            try:
                return self._pg_insert(data, embedding)
            except Exception as e:
                logger.error("Pg insert failed, trying fallback: %s", e)
                if self.mode == "pg":
                    raise

        return self._sqlite_insert(data)

    def _pg_insert(self, data: dict, embedding: list = None) -> dict:
        cur = self._pg_conn.cursor()
        cols = list(data.keys())
        placeholders = ["%s"] * len(cols)
        values = [data[k] for k in cols]

        if embedding is not None:
            cols.append("embedding")
            placeholders.append("%s")
            values.append(np.array(embedding, dtype=np.float32))

        cur.execute(
            f"INSERT INTO entries ({', '.join(cols)}) "
            f"VALUES ({', '.join(placeholders)}) "
            f"RETURNING id, who, type, layer, authority, created_at",
            values
        )
        row = cur.fetchone()
        if not row:
            raise DatabaseError("INSERT RETURNING returned no rows")
        return {
            "id": row[0], "who": row[1], "type": row[2],
            "layer": row[3], "authority": row[4],
            "created_at": row[5].isoformat() if row[5] else None,
        }

    def _sqlite_insert(self, data: dict) -> dict:
        conn = self._connect_sqlite()
        conn.execute(
            "INSERT INTO memory (fact, type) VALUES (?, ?)",
            (data["content"], data["type"])
        )
        conn.commit()
        return {"id": conn.execute("SELECT last_insert_rowid()").fetchone()[0], **data}

    def search(self, query: str, layers: list = None,
               limit: int = 20, who: str = None) -> list:
        if self.mode != "sqlite" and self._pg_conn:
            try:
                return self._pg_search(query, layers, limit, who)
            except Exception as e:
                logger.error("Pg search failed, fallback: %s", e)
                if self.mode == "pg":
                    raise
        return self._sqlite_search(query, limit)

    def _pg_search(self, query: str, layers: list = None,
                   limit: int = 20, who: str = None) -> list:
        cur = self._pg_conn.cursor()
        conditions = []
        params = []

        if layers:
            placeholders = ", ".join(f"'{l}'" for l in layers)
            conditions.append(f"layer IN ({placeholders})")
        if who:
            conditions.append("who = %s")
            params.append(who)
        if query:
            conditions.append("tsv @@ plainto_tsquery('english', %s)")
            params.append(query)

        where = " AND ".join(conditions) if conditions else "TRUE"
        sql = f"""
            SELECT id, who, type, layer, authority,
                   substring(content, 1, 200) as content_preview,
                   created_at,
                   ts_rank(tsv, plainto_tsquery('english', %s)) as rank
            FROM entries
            WHERE {where}
            ORDER BY authority DESC, rank DESC, created_at DESC
            LIMIT {limit}
        """
        all_params = [query] + params if query else params
        cur.execute(sql, all_params if params else [query] if query else [])

        results = []
        for r in cur.fetchall():
            results.append({
                "id": r[0], "who": r[1], "type": r[2],
                "layer": r[3], "authority": r[4],
                "content": r[5], "created_at": r[6].isoformat() if r[6] else None,
            })
        return results

    def _sqlite_search(self, query: str, limit: int = 20) -> list:
        conn = self._connect_sqlite()
        try:
            rows = conn.execute(
                "SELECT rowid, fact, type FROM memory_fts "
                "WHERE memory_fts MATCH ? LIMIT ?",
                (query, limit)
            ).fetchall()
            return [{"id": r[0], "content": r[1], "type": r[2], "layer": "P5"} for r in rows]
        except sqlite3.OperationalError:
            return []

    def vector_search(self, embedding: list, layers: list = None,
                      limit: int = 10) -> list:
        if self.mode == "sqlite" or self._pg_conn is None:
            return []
        cur = self._pg_conn.cursor()
        vec = np.array(embedding, dtype=np.float32)
        layer_filter = ""
        if layers:
            placeholders = ", ".join(f"'{l}'" for l in layers)
            layer_filter = f"AND layer IN ({placeholders})"

        cur.execute(
            f"""SELECT id, who, type, layer, authority,
                       substring(content, 1, 200) as preview,
                       created_at,
                       1 - (embedding <=> %s) as similarity
                FROM entries WHERE embedding IS NOT NULL {layer_filter}
                ORDER BY embedding <=> %s LIMIT {limit}""",
            (vec, vec)
        )
        results = []
        for r in cur.fetchall():
            results.append({
                "id": r[0], "who": r[1], "type": r[2],
                "layer": r[3], "authority": r[4],
                "content": r[5], "created_at": r[6].isoformat() if r[6] else None,
                "similarity": round(r[7], 4),
            })
        return results

    def hybrid_search(self, query: str, embedding: list,
                      layers: list = None, limit: int = 10) -> list:
        if self.mode == "sqlite" or self._pg_conn is None:
            return self._sqlite_search(query, limit)
        cur = self._pg_conn.cursor()
        vec = np.array(embedding, dtype=np.float32)
        layer_filter = ""
        if layers:
            placeholders = ", ".join(f"'{l}'" for l in layers)
            layer_filter = f"AND layer IN ({placeholders})"

        cur.execute(
            f"""SELECT id, who, type, layer, authority,
                       substring(content, 1, 200) as preview, created_at,
                       ts_rank(tsv, plainto_tsquery('english', %s)) as fts_score,
                       1 - (embedding <=> %s) as vec_score,
                       (ts_rank(tsv, plainto_tsquery('english', %s)) * 0.3 +
                        (1 - (embedding <=> %s)) * 0.7) as combined
                FROM entries
                WHERE (tsv @@ plainto_tsquery('english', %s) OR embedding IS NOT NULL)
                  {layer_filter}
                ORDER BY combined DESC LIMIT {limit}""",
            (query, vec, query, vec, query)
        )
        results = []
        for r in cur.fetchall():
            results.append({
                "id": r[0], "who": r[1], "type": r[2],
                "layer": r[3], "authority": r[4],
                "content": r[5], "created_at": r[6].isoformat() if r[6] else None,
                "combined_score": round(r[9], 4),
            })
        return results

    def get_layer(self, layer: str, limit: int = 50) -> list:
        return self.search("", layers=[layer], limit=limit)

    def get_layer_count(self, layer: str = None) -> dict:
        if self.mode == "sqlite" or self._pg_conn is None:
            return {"sqlite": True}
        cur = self._pg_conn.cursor()
        if layer:
            cur.execute("SELECT layer, COUNT(*) FROM entries WHERE layer = %s GROUP BY layer", (layer,))
        else:
            cur.execute("SELECT layer, COUNT(*) FROM entries GROUP BY layer ORDER BY layer")
        return {r[0]: r[1] for r in cur.fetchall()}

    def graph_query(self, entity_label: str, depth: int = 2) -> dict:
        if self.mode == "sqlite" or self._pg_conn is None:
            return {"nodes": [], "edges": []}
        cur = self._pg_conn.cursor()
        cur.execute("SELECT id, label, entity_type FROM entities WHERE label = %s", (entity_label,))
        start = cur.fetchone()
        if not start:
            return {"nodes": [], "edges": []}

        nodes = {start[0]: {"id": start[0], "label": start[1], "type": start[2]}}
        edges = []
        visited = {start[0]}
        current = {start[0]}

        for _ in range(depth):
            if not current:
                break
            placeholders = ", ".join(str(i) for i in current)
            cur.execute(
                f"""SELECT r.id, r.source_id, r.target_id, r.rel_type,
                           s.label as s_label, s.entity_type as s_type,
                           t.label as t_label, t.entity_type as t_type
                    FROM relations r
                    JOIN entities s ON r.source_id = s.id
                    JOIN entities t ON r.target_id = t.id
                    WHERE r.source_id IN ({placeholders})
                       OR r.target_id IN ({placeholders})"""
            )
            new_ids = set()
            for r in cur.fetchall():
                edges.append({"id": r[0], "source": r[1], "target": r[2], "type": r[3]})
                if r[1] not in visited:
                    nodes[r[1]] = {"id": r[1], "label": r[4], "type": r[5]}
                    new_ids.add(r[1])
                    visited.add(r[1])
                if r[2] not in visited:
                    nodes[r[2]] = {"id": r[2], "label": r[6], "type": r[7]}
                    new_ids.add(r[2])
                    visited.add(r[2])
            current = new_ids
        return {"nodes": list(nodes.values()), "edges": edges}

    def health_check(self) -> dict:
        result = {
            "mode": self.mode,
            "pg_available": self._pg_conn is not None and self.available,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            result["counts"] = self.get_layer_count()
        except Exception as e:
            result["counts_error"] = str(e)
        try:
            test = self.insert(
                layer="P6", type="health_check",
                content=f"[HEALTH_CHECK] {time.time()}",
                who="system:health"
            )
            result["write_test"] = "PASS"
            if self._pg_conn:
                cur = self._pg_conn.cursor()
                cur.execute("DELETE FROM entries WHERE id = %s", (test["id"],))
                cur.close()
        except Exception as e:
            result["write_test"] = f"FAIL: {e}"
        return result

    def close(self):
        if self._pg_conn:
            self._pg_conn.close()
        if self._sqlite_conn:
            self._sqlite_conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
