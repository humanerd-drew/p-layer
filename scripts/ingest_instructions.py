#!/usr/bin/env python3
"""Ingest instruction .md files into knowledge.db for full-text search.

Usage:
    python3 scripts/ingest_instructions.py [directory]
"""

import os
import sqlite3
import sys
from pathlib import Path

_KDIR = Path(os.environ.get("KNOWLEDGE_DB_DIR", str(Path.cwd() / ".knowledge")))
DB_DIR = _KDIR
DB = DB_DIR / "knowledge.db"


def ingest_dir(dir_path: str):
    root = Path(dir_path).resolve()
    if not root.is_dir():
        print(f"Not a directory: {dir_path}")
        return

    DB_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB))
    count = 0

    for md_file in sorted(root.rglob("*.md")):
        rel = md_file.relative_to(root)
        try:
            text = md_file.read_text(encoding="utf-8")
        except Exception as e:
            print(f"  Skip {rel}: {e}")
            continue

        db.execute(
            "INSERT INTO memory (fact, type) VALUES (?, ?)",
            (f"[{rel}]\n{text[:5000]}", f"doc:{rel.parent}"),
        )
        count += 1
        print(f"  {rel}")

    db.commit()
    total = db.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
    db.close()
    print(f"Ingested: {count} files. Total entries: {total}")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else str(Path.cwd())
    ingest_dir(target)
