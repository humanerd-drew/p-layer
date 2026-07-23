#!/usr/bin/env python3
"""Seed the knowledge database with initial entries from instruction files."""

import sqlite3
from pathlib import Path

DB_DIR = Path(__file__).resolve().parent.parent / ".knowledge"
DB = DB_DIR / "knowledge.db"


def seed():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB))
    db.execute("PRAGMA journal_mode=WAL")

    # Create memory table if not exists
    db.execute("""
        CREATE TABLE IF NOT EXISTS memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fact TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'fact',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # Create FTS5 virtual table
    try:
        db.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                fact, type,
                tokenize='porter unicode61',
                content='memory',
                content_rowid='id'
            );
            CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
                INSERT INTO memory_fts(rowid, fact, type) VALUES (new.id, new.fact, new.type);
            END;
            CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
                INSERT INTO memory_fts(memory_fts, rowid, fact, type) VALUES('delete', old.id, old.fact, old.type);
            END;
            CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
                INSERT INTO memory_fts(memory_fts, rowid, fact, type) VALUES('delete', old.id, old.fact, old.type);
                INSERT INTO memory_fts(rowid, fact, type) VALUES (new.id, new.fact, new.type);
            END;
        """)
    except sqlite3.OperationalError:
        pass  # already exists

    # Seed initial facts
    seeds = [
        ("knowledge-system initialized", "fact"),
        ("P-layer query routing: P5 > P2 > knowledge.db", "pattern"),
        ("Layer authority: P0=100, P1=80, P2=60, P3=50, P4=40, P5=30, P6=20", "fact"),
    ]
    for fact, typ in seeds:
        db.execute("INSERT INTO memory (fact, type) VALUES (?, ?)", (fact, typ))

    db.commit()
    count = db.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
    db.close()
    print(f"Seeded: {count} entries")


if __name__ == "__main__":
    seed()
