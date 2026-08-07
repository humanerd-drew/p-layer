"""PgStore — PostgreSQL backend for memcore (multi-agent / SMB phase).

Mirrors the SQLite `Store` interface: same schema semantics, same write/read
API, same governance (layer ACLs, supersede, snapshots, audit), same hybrid
recall (FTS + semantic + RRF, confidence/freshness ranked).

Backend differences, deliberate and documented:
- FTS: `to_tsvector('simple')` + ts_rank, complemented by a pg_trgm ILIKE
  search (CJK-friendly — the `simple` tokenizer has the same whitespace
  limitation as SQLite's unicode61).
- Semantic: pgvector cosine distance. The `vector` extension is optional:
  without it the store works FTS-only and says so (`semantic_available`).
- Ops jobs (reembed / consolidate / compile_wiki) are single-writer
  maintenance that runs on the SQLite store; on Pg they raise
  NotImplementedError loudly — never silently degraded.

Requires: psycopg2 (+ pgvector for semantic). DSN: MEMCORE_PG_DSN or arg.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

try:
    import psycopg2
    import psycopg2.extras
    HAS_PSYCOPG2 = True
except ImportError:  # pragma: no cover - import error path
    psycopg2 = None
    HAS_PSYCOPG2 = False

try:
    from pgvector.psycopg2 import register_vector
    HAS_PGVECTOR = True
except ImportError:
    register_vector = None
    HAS_PGVECTOR = False

from .embed import Embedder, EmbeddingError, load_embedder
from .store import (
    LAYER_AUTHORITY,
    LAYER_WRITERS,
    RELATION_CONSTRAINTS,
    WriteDenied,
    _blob_to_vec,
    _check_layer_write,
    _cosine,
    _vec_to_blob,
    compose_assemble,
    rrf_fuse,
    scan_contradictions,
    utcnow,
)

DEFAULT_DSN = os.environ.get("MEMCORE_PG_DSN", "")
DEFAULT_VECTOR_DIM = 768

_PG_KNOWLEDGE_COLS = ("id", "type", "content", "source", "session_id", "created_at",
                      "layer", "who", "confidence", "ttl_days", "superseded_by")


def _iso(value) -> str | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return value


def _row(row) -> dict:
    d = dict(row)
    d["created_at"] = _iso(d.get("created_at"))
    return d


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge (
    id BIGSERIAL PRIMARY KEY,
    type TEXT NOT NULL CHECK (type IN ('fact','decision','preference','pattern','insight')),
    content TEXT NOT NULL,
    source TEXT,
    session_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ,
    layer TEXT NOT NULL DEFAULT 'P5',
    who TEXT NOT NULL DEFAULT 'system',
    confidence REAL NOT NULL DEFAULT 1.0,
    ttl_days INTEGER,
    superseded_by BIGINT REFERENCES knowledge(id) ON DELETE SET NULL,
    superseded_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_pg_knowledge_layer ON knowledge(layer);
CREATE INDEX IF NOT EXISTS idx_pg_knowledge_created ON knowledge(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pg_knowledge_superseded ON knowledge(superseded_by);
CREATE INDEX IF NOT EXISTS idx_pg_knowledge_type ON knowledge(type);
CREATE INDEX IF NOT EXISTS idx_pg_knowledge_fts ON knowledge USING GIN (to_tsvector('simple', content));
CREATE INDEX IF NOT EXISTS idx_pg_knowledge_trgm ON knowledge USING GIN (content gin_trgm_ops);

CREATE TABLE IF NOT EXISTS embeddings (
    id BIGSERIAL PRIMARY KEY,
    knowledge_id BIGINT NOT NULL REFERENCES knowledge(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    embedding_version TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector VECTOR(768) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(knowledge_id, model, embedding_version)
);
CREATE INDEX IF NOT EXISTS idx_pg_embeddings_version ON embeddings(embedding_version);

CREATE TABLE IF NOT EXISTS episodes (
    id BIGSERIAL PRIMARY KEY,
    session_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('session','incident','retro','event')),
    payload TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    consolidated_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_pg_episodes_created ON episodes(created_at DESC);

CREATE TABLE IF NOT EXISTS entities (
    id BIGSERIAL PRIMARY KEY,
    label TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    type_parent TEXT,
    properties TEXT NOT NULL DEFAULT '{{}}',
    knowledge_id BIGINT REFERENCES knowledge(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pg_entities_type ON entities(type);

CREATE TABLE IF NOT EXISTS relations (
    id BIGSERIAL PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    target_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    properties TEXT NOT NULL DEFAULT '{{}}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(source_id, target_id, type)
);
CREATE INDEX IF NOT EXISTS idx_pg_relations_source ON relations(source_id);
CREATE INDEX IF NOT EXISTS idx_pg_relations_target ON relations(target_id);

CREATE TABLE IF NOT EXISTS rules (
    id BIGSERIAL PRIMARY KEY,
    priority INTEGER NOT NULL DEFAULT 100,
    layer TEXT NOT NULL DEFAULT 'P0',
    scope TEXT,
    condition TEXT,
    text TEXT NOT NULL,
    source TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pg_rules_priority ON rules(priority, enabled);

CREATE TABLE IF NOT EXISTS snapshots (
    id BIGSERIAL PRIMARY KEY,
    version_id TEXT NOT NULL UNIQUE,
    label TEXT,
    entry_ids TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    action TEXT NOT NULL,
    knowledge_id BIGINT,
    layer TEXT,
    who TEXT,
    detail TEXT,
    denied BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pg_audit_created ON audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pg_audit_denied ON audit_log(denied);
"""


class PgStore:
    """PostgreSQL backend with the same interface as the SQLite Store."""

    backend = "postgresql"

    def __init__(self, dsn: str | None = None, embedder: Embedder | None = None,
                 vector_dim: int = DEFAULT_VECTOR_DIM):
        self.dsn = dsn or DEFAULT_DSN
        if not self.dsn:
            raise ValueError("PgStore needs a DSN (arg or MEMCORE_PG_DSN)")
        if not HAS_PSYCOPG2:
            raise ImportError("PgStore requires psycopg2: pip install psycopg2-binary")
        self.embedder = embedder
        self.vector_dim = vector_dim
        # statement_timeout turns lock-wait hangs into clean errors (prod safety).
        self._conn = psycopg2.connect(self.dsn, connect_timeout=10,
                                      options="-c statement_timeout=30000")
        self._conn.autocommit = False
        self.semantic_available = HAS_PGVECTOR
        self.last_embed_warning: str | None = None
        if HAS_PGVECTOR:
            try:
                register_vector(self._conn)
            except Exception:
                self.semantic_available = False
        self.migrate()

    # ── connection / migrations ───────────────────────────────
    def _execute(self, sql: str, params: tuple | None = None, fetch: bool = False):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(sql, params or ())
            rows = cur.fetchall() if fetch else None
            self._conn.commit()  # never leave a read transaction open (lock-free TRUNCATE/DDL)
            return rows
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def migrate(self) -> list[int]:
        cur = self._conn.cursor()  # tuple rows: migrate uses integer indexing
        try:
            if HAS_PGVECTOR:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("SELECT version, name, checksum FROM schema_migrations")
            applied = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
            sql = _PG_SCHEMA  # fixed dim: a DB has one vector column; mismatches are handled in code
            expected = _checksum(sql)
            version = 1
            if version in applied:
                if applied[version][1] != expected:
                    raise RuntimeError("pg schema checksum mismatch: do not edit applied DDL")
            else:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s,%s,%s)",
                    (version, "initial_schema", expected),
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()
        return sorted(applied) or [1]

    # ── embedder ──────────────────────────────────────────────
    def _get_embedder(self) -> Embedder | None:
        if not self.semantic_available:
            return None
        if self.embedder is None:
            try:
                self.embedder = load_embedder()
            except ValueError:
                return None
        return self.embedder

    # ── write path ────────────────────────────────────────────
    def add_knowledge(self, content: str, type: str = "fact", source: str | None = None,
                      session_id: str | None = None, created_at: str | None = None,
                      embed: bool = True, layer: str = "P5", who: str = "system",
                      confidence: float = 1.0, ttl_days: int | None = None) -> int:
        if not content or not content.strip():
            raise ValueError("content must not be empty")
        if type not in ("fact", "decision", "preference", "pattern", "insight"):
            raise ValueError("invalid knowledge type")
        try:
            _check_layer_write(layer, who)
        except WriteDenied as exc:
            self._execute(
                "INSERT INTO audit_log (action, layer, who, detail, denied) VALUES ('write_denied',%s,%s,%s,TRUE)",
                (layer, who, str(exc)),
            )
            raise
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(
                "INSERT INTO knowledge (type, content, source, session_id, created_at, layer, who, confidence, ttl_days) "
                "VALUES (%s,%s,%s,%s,COALESCE(%s,NOW()),%s,%s,%s,%s) RETURNING id",
                (type, content, source, session_id, created_at, layer, who,
                 max(0.0, min(1.0, float(confidence))), ttl_days),
            )
            kid = int(cur.fetchone()["id"])
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()
        if embed:
            self._maybe_embed(kid, content)
        self._execute(
            "INSERT INTO audit_log (action, knowledge_id, layer, who, detail) VALUES ('remember',%s,%s,%s,%s)",
            (kid, layer, who, f"type={type} source={source}"),
        )
        return kid

    def _maybe_embed(self, kid: int, content: str) -> None:
        emb = self._get_embedder()
        if emb is None or not emb.available():
            return
        try:
            vec = emb.embed([content[:2000]])[0]
        except EmbeddingError as exc:
            self.last_embed_warning = str(exc)
            return
        if len(vec) != self.vector_dim:
            self.last_embed_warning = f"embedding dim {len(vec)} != vector({self.vector_dim})"
            return
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(
                "INSERT INTO embeddings (knowledge_id, model, embedding_version, dimensions, vector) "
                "VALUES (%s,%s,%s,%s,%s::vector) "
                "ON CONFLICT (knowledge_id, model, embedding_version) DO UPDATE SET vector = EXCLUDED.vector",
                (kid, emb.model, emb.embedding_version, len(vec), vec),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    def forget(self, knowledge_id: int, reason: str | None = None) -> bool:
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(
                "UPDATE knowledge SET superseded_by = id, superseded_reason = %s "
                "WHERE id = %s AND superseded_by IS NULL",
                (reason, knowledge_id),
            )
            ok = cur.rowcount > 0
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()
        if ok:
            self._execute(
                "INSERT INTO audit_log (action, knowledge_id, detail) VALUES ('forget',%s,%s)",
                (knowledge_id, reason or "superseded"),
            )
        return ok

    def update_knowledge(self, knowledge_id: int, content: str | None = None,
                         type: str | None = None, confidence: float | None = None,
                         layer: str | None = None, who: str | None = None) -> int:
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute("SELECT * FROM knowledge WHERE id = %s", (knowledge_id,))
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"no knowledge entry #{knowledge_id}")
            if row["superseded_by"] is not None:
                raise ValueError(f"entry #{knowledge_id} is already superseded")
            new_id = self.add_knowledge(
                content if content is not None else row["content"],
                type=type or row["type"],
                source=row["source"],
                session_id=row["session_id"],
                layer=layer or row["layer"],
                who=who or row["who"],
                confidence=confidence if confidence is not None else row["confidence"],
                ttl_days=row["ttl_days"],
            )
            self._execute(
                "UPDATE knowledge SET superseded_by = %s WHERE id = %s",
                (new_id, knowledge_id),
            )
            self._execute(
                "INSERT INTO audit_log (action, knowledge_id, layer, who, detail) VALUES ('update',%s,%s,%s,%s)",
                (knowledge_id, row["layer"], row["who"], f"superseded_by={new_id}"),
            )
            return new_id
        finally:
            cur.close()

    def snapshot_create(self, version_id: str, label: str | None = None) -> int:
        ids = [r["id"] for r in self._execute(
            "SELECT id FROM knowledge WHERE superseded_by IS NULL ORDER BY id", fetch=True)]
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(
                "INSERT INTO snapshots (version_id, label, entry_ids) VALUES (%s,%s,%s) "
                "ON CONFLICT (version_id) DO UPDATE SET label = EXCLUDED.label, "
                "entry_ids = EXCLUDED.entry_ids, created_at = NOW() RETURNING id",
                (version_id, label, json.dumps(ids)),
            )
            sid = int(cur.fetchone()["id"])
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()
        self._execute(
            "INSERT INTO audit_log (action, detail) VALUES ('snapshot_create',%s)",
            (f"version_id={version_id} entries={len(ids)}",),
        )
        return sid

    def snapshot_rollback(self, version_id: str) -> int:
        snap = self._execute(
            "SELECT id, created_at FROM snapshots WHERE version_id = %s", (version_id,), fetch=True)
        if not snap:
            raise ValueError(f"no snapshot '{version_id}'")
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(
                "UPDATE knowledge SET superseded_by = id, superseded_reason = %s "
                "WHERE superseded_by IS NULL AND created_at > %s",
                (f"rollback:{version_id}", snap[0]["created_at"]),
            )
            n = cur.rowcount
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()
        self._execute(
            "INSERT INTO audit_log (action, detail) VALUES ('snapshot_rollback',%s)",
            (f"version_id={version_id} superseded={n}",),
        )
        return n

    def add_rule(self, text: str, priority: int = 100, layer: str = "P0",
                 scope: str | None = None, condition: str | None = None,
                 source: str | None = None) -> int:
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(
                "INSERT INTO rules (priority, layer, scope, condition, text, source) "
                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                (int(priority), layer, scope, condition, text, source),
            )
            rid = int(cur.fetchone()["id"])
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()
        self._execute(
            "INSERT INTO audit_log (action, layer, detail) VALUES ('rule_add',%s,%s)",
            (layer, text[:200]),
        )
        return rid

    def add_entity(self, label: str, type: str, properties: dict | None = None,
                   knowledge_id: int | None = None) -> int:
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(
                "INSERT INTO entities (label, type, properties, knowledge_id) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (label) DO UPDATE SET type = EXCLUDED.type RETURNING id",
                (label, type, json.dumps(properties or {}, ensure_ascii=False), knowledge_id),
            )
            eid = int(cur.fetchone()["id"])
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()
        return eid

    def add_relation(self, source_id: int, target_id: int, rtype: str,
                     properties: dict | None = None) -> int:
        if rtype not in RELATION_CONSTRAINTS:
            raise ValueError(f"unknown relation type: {rtype}")
        src = self._execute(
            "SELECT id, type, label FROM entities WHERE id = %s", (source_id,), fetch=True)
        tgt = self._execute(
            "SELECT id, type, label FROM entities WHERE id = %s", (target_id,), fetch=True)
        if not src or not tgt:
            raise ValueError("relation endpoints must exist")
        constraint = RELATION_CONSTRAINTS[rtype]
        if constraint is not None:
            allowed_src, allowed_tgt = constraint
            if allowed_src is not None and src[0]["type"] not in allowed_src:
                raise ValueError(f"{src[0]['label']}({src[0]['type']}) not allowed as source of {rtype}")
            if allowed_tgt is not None and tgt[0]["type"] not in allowed_tgt:
                raise ValueError(f"{tgt[0]['label']}({tgt[0]['type']}) not allowed as target of {rtype}")
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(
                "INSERT INTO relations (source_id, target_id, type, properties) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (source_id, target_id, type) DO NOTHING RETURNING id",
                (source_id, target_id, rtype, json.dumps(properties or {}, ensure_ascii=False)),
            )
            row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()
        return int(row["id"]) if row else 0

    def record_episode(self, kind: str, payload, session_id: str | None = None) -> int:
        if kind not in ("session", "incident", "retro", "event"):
            raise ValueError("kind must be one of session/incident/retro/event")
        text = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(
                "INSERT INTO episodes (session_id, kind, payload) VALUES (%s,%s,%s) RETURNING id",
                (session_id, kind, text),
            )
            eid = int(cur.fetchone()["id"])
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()
        return eid

    # ── read path ─────────────────────────────────────────────
    def _rows_by_ids(self, ids: list[int]) -> dict[int, dict]:
        if not ids:
            return {}
        marks = ",".join(["%s"] * len(ids))
        rows = self._execute(
            f"SELECT {', '.join(_PG_KNOWLEDGE_COLS)} FROM knowledge "
            f"WHERE id IN ({marks}) AND superseded_by IS NULL",
            tuple(ids),
            fetch=True,
        )
        return {r["id"]: _row(r) for r in rows}

    def fts_search(self, query: str, limit: int = 30) -> list[dict]:
        terms = [w for w in query.split() if len(w) > 1]
        if not terms:
            return []
        q = " ".join(terms)
        cols = ", ".join(_PG_KNOWLEDGE_COLS)
        rows = self._execute(
            f"SELECT {cols} FROM knowledge k "
            "WHERE to_tsvector('simple', k.content) @@ plainto_tsquery('simple', %s) "
            "AND k.superseded_by IS NULL "
            "ORDER BY ts_rank(to_tsvector('simple', k.content), plainto_tsquery('simple', %s)) DESC, k.id "
            "LIMIT %s",
            (q, q, limit),
            fetch=True,
        )
        results = [_row(r) for r in rows]
        # pg_trgm ILIKE complement — CJK and substring matches the FTS misses.
        found = {r["id"] for r in results}
        for term in terms:
            if len(results) >= limit:
                break
            extra = self._execute(
                f"SELECT {cols} FROM knowledge k "
                "WHERE k.content ILIKE %s AND k.superseded_by IS NULL AND k.id NOT IN (SELECT unnest(%s::bigint[])) "
                "ORDER BY k.id LIMIT %s",
                (f"%{term}%", list(found) or [0], limit - len(results)),
                fetch=True,
            )
            for r in extra:
                if r["id"] in found:
                    continue
                found.add(r["id"])
                results.append(_row(r))
        return results

    def semantic_search(self, query: str, limit: int = 30) -> list[tuple[float, int]]:
        emb = self._get_embedder()
        if emb is None or not emb.available():
            return []
        try:
            qvec = emb.embed([query])[0]
        except EmbeddingError:
            return []
        if len(qvec) != self.vector_dim:
            return []
        rows = self._execute(
            "SELECT knowledge_id, 1 - (vector <=> %s::vector) AS sim "
            "FROM embeddings WHERE embedding_version = %s ORDER BY sim DESC LIMIT %s",
            (qvec, emb.embedding_version, limit),
            fetch=True,
        )
        return [(float(r["sim"]), int(r["knowledge_id"])) for r in rows if float(r["sim"]) > 0.15]

    def recall(self, query: str, limit: int = 10, use_semantic: bool | None = None,
               serendipity: bool = False) -> list[dict]:
        if use_semantic is None:
            use_semantic = self.semantic_available and self._get_embedder() is not None
        fts = self.fts_search(query, limit * 3)
        sem = self.semantic_search(query, limit * 3) if use_semantic else []
        out = rrf_fuse(fts, sem, limit, self._rows_by_ids)
        if serendipity and out:
            import random

            if random.random() < 0.05:
                marks = ",".join(["%s"] * len(out))
                row = self._execute(
                    f"SELECT {', '.join(_PG_KNOWLEDGE_COLS)} FROM knowledge "
                    f"WHERE superseded_by IS NULL AND id NOT IN ({marks}) ORDER BY RANDOM() LIMIT 1",
                    tuple(r["id"] for r in out),
                    fetch=True,
                )
                if row:
                    out.append(_row(row[0]) | {"score": None, "semantic_score": None, "_serendipity": True})
        return out

    def enabled_rules(self, limit: int = 100) -> list[dict]:
        rows = self._execute(
            "SELECT id, priority, layer, scope, condition, text, source FROM rules "
            "WHERE enabled = TRUE ORDER BY priority ASC, id ASC LIMIT %s",
            (limit,),
            fetch=True,
        )
        return [dict(r) for r in rows]

    def recent_knowledge(self, limit: int = 20) -> list[dict]:
        rows = self._execute(
            f"SELECT {', '.join(_PG_KNOWLEDGE_COLS)} FROM knowledge k "
            "WHERE k.superseded_by IS NULL ORDER BY k.created_at DESC, k.id DESC LIMIT %s",
            (limit,),
            fetch=True,
        )
        return [_row(r) for r in rows]

    def assemble(self, budget_chars: int = 12000, include: tuple[str, ...] = ("rules", "recent")) -> str:
        rules = self.enabled_rules() if "rules" in include else []
        recent = self.recent_knowledge(limit=50) if "recent" in include else []
        return compose_assemble(rules, recent, budget_chars)

    def audit_log(self, limit: int = 50, denied_only: bool = False) -> list[dict]:
        sql = ("SELECT id, action, knowledge_id, layer, who, detail, denied, created_at FROM audit_log")
        if denied_only:
            sql += " WHERE denied = TRUE"
        sql += " ORDER BY id DESC LIMIT %s"
        rows = self._execute(sql, (limit,), fetch=True)
        return [_row(r) for r in rows]

    def contradictions(self) -> list[dict]:
        rules = self.enabled_rules(limit=500)
        rows = self._execute(
            "SELECT id, layer, type, content FROM knowledge k WHERE k.superseded_by IS NULL", fetch=True)
        return scan_contradictions(rules, [dict(r) for r in rows])

    # ── graph & inference ─────────────────────────────────────
    def _find_entities(self, query: str, limit: int = 20) -> list[dict]:
        rows = self._execute(
            "SELECT id, label, type, properties FROM entities WHERE label ILIKE %s "
            "ORDER BY CASE WHEN label = %s THEN 0 WHEN label ILIKE %s THEN 1 ELSE 2 END, label LIMIT %s",
            (f"%{query}%", query, f"{query}%", limit),
            fetch=True,
        )
        return [dict(r) for r in rows]

    def _traverse(self, entity_id: int, direction: str = "out", depth: int = 3,
                  rel_type: str | None = None) -> list[dict]:
        if direction not in ("out", "in"):
            raise ValueError("direction must be 'out' or 'in'")
        depth = max(1, min(int(depth), 10))
        rel_filter = "AND r.type = %s" if rel_type else ""
        rel_params = [rel_type] if rel_type else []
        if direction == "out":
            base_next, base_where = "r.target_id", "r.source_id = %s"
            rec_join = "JOIN relations r ON r.source_id = p.next_id"
        else:
            base_next, base_where = "r.source_id", "r.target_id = %s"
            rec_join = "JOIN relations r ON r.target_id = p.next_id"
        sql = f"""
            WITH RECURSIVE path(rel_id, rel_type, next_id, lvl) AS (
                SELECT r.id, r.type, {base_next}, 1
                FROM relations r WHERE {base_where} {rel_filter}
                UNION
                SELECT r.id, r.type, {base_next}, p.lvl + 1
                FROM path p {rec_join}
                WHERE p.lvl < %s {rel_filter}
            )
            SELECT p.rel_id, p.rel_type AS relation, e.id AS entity_id, e.label,
                   e.type AS entity_type, p.lvl AS level
            FROM path p JOIN entities e ON e.id = p.next_id
            ORDER BY p.lvl, p.rel_id
        """
        rows = self._execute(sql, tuple([entity_id] + rel_params + [depth] + rel_params), fetch=True)
        return [dict(r) for r in rows]

    def graph_explore(self, query: str, depth: int = 2, limit: int = 20) -> dict:
        out = {"query": query, "entities": []}
        for e in self._find_entities(query, limit):
            seen: set[tuple] = set()
            neighbors = []
            for n in self._traverse(e["id"], "out", depth):
                key = (n["relation"], n["entity_id"])
                if key in seen:
                    continue
                seen.add(key)
                neighbors.append(n)
            out["entities"].append({"id": e["id"], "label": e["label"], "type": e["type"], "neighbors": neighbors})
        return out

    def graph_trace(self, query: str, depth: int = 4) -> dict:
        out = {"query": query, "trace": []}
        for e in self._find_entities(query):
            out["trace"].append({
                "entity": e["label"], "type": e["type"],
                "inbound": self._traverse(e["id"], "in", depth),
                "outbound": self._traverse(e["id"], "out", depth),
            })
        return out

    def graph_rca(self, query: str, depth: int = 3) -> dict:
        out = {"query": query, "root_causes": []}
        for e in self._find_entities(query):
            if e["type"] not in ("incident", "pattern"):
                continue
            timeline = (
                [{"type": "cause", "entity": c["label"], "entity_type": c["entity_type"],
                  "level": c["level"]} for c in self._traverse(e["id"], "in", depth, rel_type="caused")]
                + [{"type": "fix", "entity": f["label"], "entity_type": f["entity_type"],
                    "level": f["level"]} for f in self._traverse(e["id"], "out", depth, rel_type="fixed_by")]
            )
            out["root_causes"].append({"incident": {"id": e["id"], "label": e["label"], "type": e["type"]},
                                       "timeline": timeline})
        return out

    def transitive_closure(self, entity_id: int, rel_type: str = "depends_on",
                           direction: str = "out", depth: int = 5) -> list[dict]:
        return self._traverse(entity_id, direction, depth, rel_type=rel_type)

    # ── ops jobs: SQLite-only (loud, never silent) ────────────
    def reembed(self, *args, **kwargs):
        raise NotImplementedError("reembed is a single-writer maintenance job — run it on the SQLite store")

    def consolidate(self, *args, **kwargs):
        raise NotImplementedError("consolidate is a single-writer maintenance job — run it on the SQLite store")

    def compile_wiki(self, *args, **kwargs):
        raise NotImplementedError("compile_wiki is a single-writer maintenance job — run it on the SQLite store")

    def stats(self) -> dict:
        def count(table: str) -> int:
            rows = self._execute(f"SELECT COUNT(*) AS c FROM {table}", fetch=True)
            return int(rows[0]["c"])

        by_type = {}
        by_layer = {}
        for r in self._execute("SELECT type, COUNT(*) AS c FROM knowledge GROUP BY type", fetch=True):
            by_type[r["type"]] = r["c"]
        for r in self._execute("SELECT layer, COUNT(*) AS c FROM knowledge GROUP BY layer", fetch=True):
            by_layer[r["layer"]] = r["c"]
        by_version = {}
        if self.semantic_available:
            for r in self._execute("SELECT embedding_version, COUNT(*) AS c FROM embeddings GROUP BY embedding_version", fetch=True):
                by_version[r["embedding_version"]] = r["c"]
        emb = self._get_embedder()
        return {
            "backend": "postgresql",
            "knowledge": count("knowledge"),
            "active": count("knowledge") - int(
                self._execute("SELECT COUNT(*) AS c FROM knowledge WHERE superseded_by IS NOT NULL", fetch=True)[0]["c"]),
            "by_type": by_type,
            "by_layer": by_layer,
            "embeddings": count("embeddings") if self.semantic_available else 0,
            "embeddings_by_version": by_version,
            "episodes": count("episodes"),
            "entities": count("entities"),
            "relations": count("relations"),
            "rules": count("rules"),
            "snapshots": count("snapshots"),
            "audit": count("audit_log"),
            "schema_version": 1,
            "embedder": emb.name if emb is not None else None,
            "semantic_available": self.semantic_available,
            "embed_warning": self.last_embed_warning,
        }
