#!/usr/bin/env python3
"""Compile raw knowledge entries into condensed wiki pages.

Reads from knowledge.db, writes structured Markdown to wiki/compiled/.
Each page = one concept, condensed to key points + relations.

Usage:
    python3 scripts/wiki_compile.py [--output <dir>] [--limit <N>]
"""

import argparse
import json
import os
import sqlite3
from collections import defaultdict
from pathlib import Path

_KDIR = Path(os.environ.get("KNOWLEDGE_DB_DIR", str(Path.cwd() / ".knowledge")))
DB_DIR = _KDIR
DB = DB_DIR / "knowledge.db"
DEFAULT_OUTPUT = Path(os.environ.get("KNOWLEDGE_WIKI_DIR", str(Path.cwd() / "wiki" / "compiled")))


def get_entity_relations(db, entity_id: int) -> list:
    rows = db.execute(
        """SELECT r.type, e.label, e.type
           FROM relations r
           JOIN entities e ON r.target_id = e.id
           WHERE r.source_id = ?
           UNION
           SELECT r.type, e.label, e.type
           FROM relations r
           JOIN entities e ON r.source_id = e.id
           WHERE r.target_id = ?""",
        (entity_id, entity_id),
    ).fetchall()
    return [{"type": r[0], "label": r[1], "entity_type": r[2]} for r in rows]


def compile_wiki(output_dir: Path, limit: int = 50):
    DB_DIR.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not DB.exists():
        print("knowledge.db not found. Run seed_knowledge_db.py first.")
        return

    db = sqlite3.connect(str(DB))

    # Collect distinct types/concepts from entities
    entities = db.execute(
        "SELECT id, label, type, properties, created_at FROM entities ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()

    index = []
    for ent in entities:
        eid, label, etype, props, created = ent
        relations = get_entity_relations(db, eid)
        props_dict = json.loads(props) if props else {}
        description = props_dict.get("description", "")

        slug = label.lower().replace(" ", "-").replace("/", "-")
        page_path = output_dir / f"{slug}.md"

        rel_lines = "\n".join(
            f"- {r['type']}: [[{r['label']}]] ({r['entity_type']})"
            for r in relations
        )

        content = f"# {label}\n\n"
        content += f"- **Type:** {etype}\n"
        if created:
            content += f"- **Created:** {created}\n"
        if description:
            content += f"\n{description}\n"
        if relations:
            content += f"\n## Relations\n{rel_lines}\n"

        page_path.write_text(content, encoding="utf-8")

        index.append({
            "slug": slug,
            "label": label,
            "type": etype,
            "relation_count": len(relations),
        })

    # Write INDEX.json
    index_path = output_dir / "INDEX.json"
    index_path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")

    db.close()
    print(f"Compiled: {len(entities)} pages to {output_dir}")
    print(f"Index: {index_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compile knowledge.db to wiki pages")
    parser.add_argument("--output", type=str, default=str(DEFAULT_OUTPUT))
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    compile_wiki(Path(args.output), args.limit)
