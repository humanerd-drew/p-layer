#!/usr/bin/env python3
"""Initialize/validate the ontology layer — type hierarchy + relation constraints.

Usage:
    python3 scripts/ontology_setup.py                    # init + validate
    python3 scripts/ontology_setup.py add-entity <label> <type> [props_json]
    python3 scripts/ontology_setup.py add-relation <sid> <tid> <type> [props_json]
    python3 scripts/ontology_setup.py query [label] [type] [limit]
"""

import json
import sqlite3
from pathlib import Path

from knowledge_system.core.ontology import TYPE_HIERARCHY, RELATION_CONSTRAINTS

DB_DIR = Path(__file__).resolve().parent.parent / ".knowledge"
DB = DB_DIR / "knowledge.db"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS entity_types (
    type TEXT PRIMARY KEY,
    parent_type TEXT REFERENCES entity_types(type) ON DELETE SET NULL,
    description TEXT DEFAULT ''
);
REPLACE INTO entity_types (type, parent_type, description) VALUES
    ('artifact', NULL, 'Artifacts: docs, code'),
    ('doc', 'artifact', 'Documentation, wiki pages'),
    ('code', 'artifact', 'Code snippets, PRs'),
    ('project', 'artifact', 'Projects'),
    ('agent', NULL, 'Agent-related objects'),
    ('persona', 'agent', 'Agent profiles'),
    ('tool', 'agent', 'External tools'),
    ('script', 'agent', 'Automation scripts'),
    ('skill', 'agent', 'Skills'),
    ('decision', NULL, 'Decisions made'),
    ('pattern', 'decision', 'Recurring patterns'),
    ('preference', 'decision', 'Preferences'),
    ('event', NULL, 'Events that occurred'),
    ('incident', 'event', 'Incidents, problems'),
    ('session', 'event', 'Conversation sessions'),
    ('knowledge', NULL, 'Knowledge'),
    ('concept', 'knowledge', 'Concepts'),
    ('paper', 'knowledge', 'Papers'),
    ('reference', 'knowledge', 'External references'),
    ('meta', NULL, 'System management'),
    ('category', 'meta', 'Categories'),
    ('_task', 'meta', 'Tasks'),
    ('fact', 'meta', 'Flat facts');

CREATE TABLE IF NOT EXISTS relation_constraints (
    relation TEXT PRIMARY KEY,
    source_types TEXT,
    target_types TEXT
);
REPLACE INTO relation_constraints (relation, source_types, target_types) VALUES
    ('depends_on', NULL, '["tool","script","skill"]'),
    ('fixed_by', '["incident"]', '["pattern","decision"]'),
    ('caused', '["decision","pattern"]', '["incident"]'),
    ('led_to', '["decision","pattern","preference"]', '["decision","pattern","preference"]'),
    ('implements', '["script"]', '["pattern","decision"]'),
    ('contradicts', '["decision","pattern","preference"]', '["decision","pattern","preference"]'),
    ('cites', '["paper"]', '["paper"]'),
    ('references', NULL, NULL),
    ('relates_to', NULL, NULL),
    ('subtype_of', NULL, NULL),
    ('belongs_to', NULL, NULL);

CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    type TEXT NOT NULL REFERENCES entity_types(type),
    type_parent TEXT REFERENCES entity_types(type),
    properties TEXT DEFAULT '{{}}',
    knowledge_id INTEGER REFERENCES knowledge(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    type TEXT NOT NULL REFERENCES relation_constraints(relation),
    properties TEXT DEFAULT '{{}}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);
CREATE INDEX IF NOT EXISTS idx_entities_label ON entities(label);
CREATE INDEX IF NOT EXISTS idx_relations_source ON relations(source_id);
CREATE INDEX IF NOT EXISTS idx_relations_target ON relations(target_id);
CREATE INDEX IF NOT EXISTS idx_relations_type ON relations(type);
"""


def validate_relation_type(db, source_type, target_type, rtype):
    constraint = RELATION_CONSTRAINTS.get(rtype)
    if constraint is None:
        return True, "ok (no constraint)"
    allowed_src, allowed_tgt = constraint
    if allowed_src is None or allowed_tgt is None:
        return True, "ok (any type)"

    def type_or_parent(t):
        parent = TYPE_HIERARCHY.get(t)
        return {t, parent} if parent else {t}

    if not (type_or_parent(source_type) & set(allowed_src)):
        return False, f"{source_type} not in allowed source types for {rtype}"
    if not (type_or_parent(target_type) & set(allowed_tgt)):
        return False, f"{target_type} not in allowed target types for {rtype}"
    return True, "ok"


def main():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB))
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    db.commit()

    # Validate existing relations
    violations = []
    for row in db.execute("""
        SELECT r.id, r.type, e1.type, e2.type, e1.label, e2.label
        FROM relations r
        JOIN entities e1 ON r.source_id = e1.id
        JOIN entities e2 ON r.target_id = e2.id
    """).fetchall():
        rid, rtype, src_type, tgt_type, src_label, tgt_label = row
        valid, reason = validate_relation_type(db, src_type, tgt_type, rtype)
        if not valid:
            violations.append(f"#{rid} {src_label}({src_type}) --{rtype}--> {tgt_label}({tgt_type}): {reason}")

    count = db.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    rcount = db.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    db.close()

    parts = [f"ontology: {count} entities, {rcount} relations"]
    if violations:
        parts.append(f"!! {len(violations)} violations:")
        parts.extend(f"  {v}" for v in violations[:5])
    print("\n".join(parts))


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        main()
    elif sys.argv[1] == "add-entity":
        DB_DIR.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(DB))
        label, etype = sys.argv[2], sys.argv[3]
        props = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}
        cur = db.execute(
            "INSERT INTO entities (label, type, properties) VALUES (?, ?, ?)",
            (label, etype, json.dumps(props, ensure_ascii=False)),
        )
        eid = cur.lastrowid
        db.commit()
        db.close()
        print(json.dumps({"id": eid, "label": label, "type": etype}))
    elif sys.argv[1] == "add-relation":
        DB_DIR.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(DB))
        sid, tid, rtype = int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
        props = json.loads(sys.argv[5]) if len(sys.argv) > 5 else {}
        src = db.execute("SELECT type, label FROM entities WHERE id = ?", (sid,)).fetchone()
        tgt = db.execute("SELECT type, label FROM entities WHERE id = ?", (tid,)).fetchone()
        if src and tgt:
            valid, reason = validate_relation_type(db, src[0], tgt[0], rtype)
            if not valid:
                db.close()
                raise ValueError(f"Relation blocked: {src[1]}({src[0]}) --{rtype}--> {tgt[1]}({tgt[0]}) — {reason}")
        cur = db.execute(
            "INSERT INTO relations (source_id, target_id, type, properties) VALUES (?, ?, ?, ?)",
            (sid, tid, rtype, json.dumps(props, ensure_ascii=False)),
        )
        rid = cur.lastrowid
        db.commit()
        db.close()
        print(json.dumps({"id": rid, "source": sid, "target": tid, "type": rtype}))
    elif sys.argv[1] == "query":
        DB_DIR.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(DB))
        label = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
        etype = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else None
        limit = int(sys.argv[4]) if len(sys.argv) > 4 else 20
        parts, params = [], []
        if label:
            parts.append("label LIKE ?")
            params.append(f"%{label}%")
        if etype:
            parts.append("type = ?")
            params.append(etype)
        where = " AND ".join(parts) if parts else "1"
        rows = db.execute(
            f"SELECT id, label, type, properties, created_at FROM entities WHERE {where} ORDER BY id DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        db.close()
        print(json.dumps([{"id": r[0], "label": r[1], "type": r[2]} for r in rows], indent=2))
    else:
        main()
