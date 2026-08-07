"""Forward-only, checksummed schema migrations.

Every schema change is a new entry in MIGRATIONS. Never edit an applied
migration: the checksum is verified on every open and a mismatch raises
RuntimeError. This is the discipline drewgent lacked (CREATE IF NOT EXISTS
everywhere, no versioning, two parallel schemas).
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone

MIGRATIONS: list[tuple[int, str, str]] = []


def _register(version: int, name: str, sql: str) -> None:
    if any(v == version for v, _, _ in MIGRATIONS):
        raise RuntimeError(f"duplicate migration version {version}")
    MIGRATIONS.append((version, name, sql))


_register(
    1,
    "initial_schema",
    """
CREATE TABLE knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL CHECK (type IN ('fact','decision','preference','pattern','insight')),
    content TEXT NOT NULL,
    source TEXT,
    session_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE INDEX idx_knowledge_type ON knowledge(type);
CREATE INDEX idx_knowledge_created ON knowledge(created_at DESC);

CREATE VIRTUAL TABLE knowledge_fts USING fts5(content, type, tokenize='unicode61');

CREATE TABLE embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    knowledge_id INTEGER NOT NULL REFERENCES knowledge(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    embedding_version TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector BLOB NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(knowledge_id, model, embedding_version)
);
CREATE INDEX idx_embeddings_version ON embeddings(embedding_version);

CREATE TABLE episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('session','incident','retro','event')),
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_episodes_created ON episodes(created_at DESC);

CREATE TABLE entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    type_parent TEXT,
    properties TEXT NOT NULL DEFAULT '{}',
    knowledge_id INTEGER REFERENCES knowledge(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_entities_type ON entities(type);

CREATE TABLE relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    properties TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(source_id, target_id, type)
);
CREATE INDEX idx_relations_source ON relations(source_id);
CREATE INDEX idx_relations_target ON relations(target_id);

CREATE TABLE rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    priority INTEGER NOT NULL DEFAULT 100,
    layer TEXT NOT NULL DEFAULT 'P0',
    scope TEXT,
    condition TEXT,
    text TEXT NOT NULL,
    source TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_rules_priority ON rules(priority, enabled);
""",
)
_register(
    2,
    "layer_governance",
    """
ALTER TABLE knowledge ADD COLUMN layer TEXT NOT NULL DEFAULT 'P5';
ALTER TABLE knowledge ADD COLUMN who TEXT NOT NULL DEFAULT 'system';
ALTER TABLE knowledge ADD COLUMN confidence REAL NOT NULL DEFAULT 1.0;
ALTER TABLE knowledge ADD COLUMN ttl_days INTEGER;
ALTER TABLE knowledge ADD COLUMN superseded_by INTEGER REFERENCES knowledge(id) ON DELETE SET NULL;
ALTER TABLE knowledge ADD COLUMN superseded_reason TEXT;
CREATE INDEX idx_knowledge_layer ON knowledge(layer);
CREATE INDEX idx_knowledge_superseded ON knowledge(superseded_by);

CREATE TABLE snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id TEXT NOT NULL UNIQUE,
    label TEXT,
    entry_ids TEXT NOT NULL,
    created_at TEXT NOT NULL
);
""",
)
_register(
    3,
    "audit_log",
    """
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    knowledge_id INTEGER,
    layer TEXT,
    who TEXT,
    detail TEXT,
    denied INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_audit_created ON audit_log(created_at DESC);
CREATE INDEX idx_audit_knowledge ON audit_log(knowledge_id);
CREATE INDEX idx_audit_denied ON audit_log(denied);
""",
)
_register(
    4,
    "consolidation",
    """
ALTER TABLE episodes ADD COLUMN consolidated_at TEXT;
""",
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _split_statements(sql: str) -> list[str]:
    # Schema statements are one-per-line ending in ';' with no embedded
    # semicolons, so a plain split is safe here.
    return [s.strip() for s in sql.split(";") if s.strip()]


def applied_versions(db: sqlite3.Connection) -> set[int]:
    try:
        rows = db.execute("SELECT version FROM schema_migrations").fetchall()
    except sqlite3.OperationalError:
        return set()
    return {r[0] for r in rows}


def migrate(db: sqlite3.Connection, target: int | None = None) -> list[int]:
    """Apply pending migrations and verify checksums of applied ones."""
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied = applied_versions(db)
    stored = {
        r[0]: (r[1], r[2])
        for r in db.execute("SELECT version, name, checksum FROM schema_migrations").fetchall()
    }
    for version, name, sql in sorted(MIGRATIONS):
        if target is not None and version > target:
            continue
        expected = _checksum(sql)
        if version in applied:
            if stored[version][1] != expected:
                raise RuntimeError(
                    f"migration {version} ({name}) checksum mismatch: "
                    "schema was edited after it was applied — add a new migration instead"
                )
            continue
        try:
            db.execute("BEGIN")
            for stmt in _split_statements(sql):
                db.execute(stmt)
            db.execute(
                "INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (?,?,?,?)",
                (version, name, expected, _utcnow()),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
    done = {v for v, _, _ in MIGRATIONS if target is None or v <= target}
    return sorted(applied | done)
