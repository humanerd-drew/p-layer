#!/usr/bin/env python3
"""Inference engine for entity graph — transitive closure, backtrace, contradiction detection.

Usage:
    python3 scripts/inference.py transitive --entity <name> --type <relation_type>
    python3 scripts/inference.py backtrace --entity <entity>
    python3 scripts/inference.py contradictions
    python3 scripts/inference.py all
"""

import json
import os
import sqlite3
from pathlib import Path

_KDIR = Path(os.environ.get("KNOWLEDGE_DB_DIR", str(Path.cwd() / ".knowledge")))
DB_DIR = _KDIR
DB = DB_DIR / "knowledge.db"


def get_entity_id(name):
    db = sqlite3.connect(str(DB))
    row = db.execute("SELECT id, type FROM entities WHERE label = ?", (name,)).fetchone()
    db.close()
    return row


def transitive_closure(name, rel_type="depends_on", max_depth=10):
    """Walk transitive dependencies."""
    start = get_entity_id(name)
    if not start:
        return {"error": f"Entity '{name}' not found"}

    db = sqlite3.connect(str(DB))
    visited = {start[0]}
    chain = []
    current = {start[0]}

    for depth in range(max_depth):
        if not current:
            break
        placeholders = ", ".join("?" for _ in current)
        rows = db.execute(
            f"""SELECT DISTINCT r.target_id, e.label, e.type, r.type
                FROM relations r
                JOIN entities e ON r.target_id = e.id
                WHERE r.source_id IN ({placeholders})
                AND r.type = ? AND r.target_id NOT IN ({placeholders})""",
            list(current) + [rel_type] + list(visited)
        ).fetchall()
        new = set()
        for row in rows:
            chain.append({"depth": depth + 1, "label": row[1], "type": row[2], "via": row[3]})
            if row[0] not in visited:
                new.add(row[0])
                visited.add(row[0])
        current = new

    db.close()
    return {"entity": name, "chain": chain}


def backtrace(name, max_depth=10):
    """Find all entities that can reach this entity."""
    start = get_entity_id(name)
    if not start:
        return {"error": f"Entity '{name}' not found"}

    db = sqlite3.connect(str(DB))
    visited = {start[0]}
    trace = []
    current = {start[0]}

    for depth in range(max_depth):
        if not current:
            break
        placeholders = ", ".join("?" for _ in current)
        rows = db.execute(
            f"""SELECT DISTINCT r.source_id, e.label, e.type, r.type
                FROM relations r
                JOIN entities e ON r.source_id = e.id
                WHERE r.target_id IN ({placeholders})
                AND r.source_id NOT IN ({placeholders})""",
            list(current) + list(visited)
        ).fetchall()
        new = set()
        for row in rows:
            trace.append({"depth": depth + 1, "label": row[1], "type": row[2], "via": row[3]})
            if row[0] not in visited:
                new.add(row[0])
                visited.add(row[0])
        current = new

    db.close()
    return {"entity": name, "backtrace": trace}


def contradictions():
    """Find contradictory decisions/patterns."""
    db = sqlite3.connect(str(DB))
    rows = db.execute("""
        SELECT r.id, s.label, s.type, t.label, t.type, r.properties
        FROM relations r
        JOIN entities s ON r.source_id = s.id
        JOIN entities t ON r.target_id = t.id
        WHERE r.type = 'contradicts'
    """).fetchall()
    db.close()
    return [{
        "id": r[0], "source": r[1], "source_type": r[2],
        "target": r[3], "target_type": r[4], "properties": r[5],
    } for r in rows]


def all_():
    return {
        "contradictions": contradictions(),
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print(json.dumps(all_(), indent=2))
    elif sys.argv[1] == "transitive":
        name = sys.argv[3] if "--entity" in sys.argv else None
        rtype = sys.argv[3] if "--type" in sys.argv else None
        print(json.dumps(transitive_closure(name, rtype or "depends_on"), indent=2))
    elif sys.argv[1] == "backtrace":
        name = sys.argv[3] if "--entity" in sys.argv else None
        print(json.dumps(backtrace(name), indent=2))
    elif sys.argv[1] == "contradictions":
        print(json.dumps(contradictions(), indent=2))
    elif sys.argv[1] == "all":
        print(json.dumps(all_(), indent=2))
    else:
        print(json.dumps(all_(), indent=2))
