-- knowledge-system: Database Schema
-- PostgreSQL primary, SQLite fallback

-- ── PostgreSQL Schema ─────────────────────────────────────────────

-- Run on PostgreSQL:
--   psql -d yourdb -f schema/knowledge.sql

CREATE TABLE IF NOT EXISTS entries (
    id SERIAL PRIMARY KEY,
    who TEXT NOT NULL DEFAULT 'system',
    type TEXT NOT NULL DEFAULT 'fact',
    layer TEXT NOT NULL DEFAULT 'P5',
    authority INTEGER NOT NULL DEFAULT 30,
    content TEXT NOT NULL,
    source TEXT DEFAULT '',
    source_path TEXT DEFAULT '',
    embedding vector(1536),
    confidence REAL DEFAULT 1.0,
    ttl_days INTEGER DEFAULT 90,
    version_id TEXT DEFAULT 'latest',
    access_count INTEGER DEFAULT 0,
    last_accessed_at TIMESTAMPTZ,
    superseded_by INTEGER REFERENCES entries(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_entries_layer ON entries(layer);
CREATE INDEX IF NOT EXISTS idx_entries_type ON entries(type);
CREATE INDEX IF NOT EXISTS idx_entries_who ON entries(who);
CREATE INDEX IF NOT EXISTS idx_entries_created ON entries(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_entries_superseded ON entries(superseded_by);
CREATE INDEX IF NOT EXISTS idx_entries_embedding ON entries USING ivfflat (embedding vector_cosine_ops);

ALTER TABLE entries ADD COLUMN IF NOT EXISTS tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', coalesce(content, ''))) STORED;

CREATE INDEX IF NOT EXISTS idx_entries_tsv ON entries USING GIN(tsv);

CREATE TABLE IF NOT EXISTS memory_snapshots (
    id SERIAL PRIMARY KEY,
    version_id TEXT UNIQUE NOT NULL,
    label TEXT DEFAULT '',
    entry_ids INTEGER[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS entities (
    id SERIAL PRIMARY KEY,
    label TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    properties JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_entities_label ON entities(label);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);

CREATE TABLE IF NOT EXISTS relations (
    id SERIAL PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    rel_type TEXT NOT NULL,
    properties JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_relations_source ON relations(source_id);
CREATE INDEX IF NOT EXISTS idx_relations_target ON relations(target_id);
CREATE INDEX IF NOT EXISTS idx_relations_type ON relations(rel_type);

-- ── SQLite Schema ────────────────────────────────────────────────

-- The SQLite fallback is auto-created by KnowledgeDB.
-- Minimal schema for FTS5 search:

-- CREATE VIRTUAL TABLE memory_fts USING fts5(
--     fact, type,
--     tokenize='porter unicode61'
-- );
--
-- CREATE TABLE IF NOT EXISTS memory (
--     id INTEGER PRIMARY KEY AUTOINCREMENT,
--     fact TEXT NOT NULL,
--     type TEXT NOT NULL DEFAULT 'fact',
--     created_at TEXT NOT NULL DEFAULT (datetime('now'))
-- );
