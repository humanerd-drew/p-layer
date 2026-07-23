#!/usr/bin/env python3
"""Ingest a single fact into knowledge.db.

Usage:
    python3 scripts/ingest_fact.py "some fact" [type]
"""

import os
import sqlite3
import sys
from pathlib import Path

_KDIR = Path(os.environ.get("KNOWLEDGE_DB_DIR", str(Path.cwd() / ".knowledge")))
DB_DIR = _KDIR
DB = DB_DIR / "knowledge.db"


def ingest(fact: str, typ: str = "fact"):
    DB_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB))
    db.execute("INSERT INTO memory (fact, type) VALUES (?, ?)", (fact, typ))
    db.commit()
    eid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.close()
    print(f"Ingested: #{eid} ({typ}) {fact[:60]}...")
    return eid


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/ingest_fact.py <fact> [type]")
        sys.exit(1)
    ingest(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "fact")
