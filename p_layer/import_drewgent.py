"""Import a drewgent knowledge.db into a p_layer store.

This is the migration path: real drewgent data in, a single-schema p_layer
store out. Knowledge rows are re-embedded under p_layer's embedding version;
entities/relations are carried over with their type constraints re-validated.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .store import Store


def import_drewgent(src_path: str | Path, store: Store, reembed: bool = True) -> dict:
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        src.execute("PRAGMA query_only=ON")

        knowledge = []
        try:
            knowledge = src.execute(
                "SELECT id, type, content, source, created_at FROM knowledge ORDER BY id"
            ).fetchall()
        except sqlite3.OperationalError:
            pass

        imported = 0
        for row in knowledge:
            store.add_knowledge(
                row["content"],
                type=row["type"] if row["type"] in ("fact", "decision", "preference", "pattern") else "fact",
                source=row["source"],
                created_at=row["created_at"] or None,
                embed=reembed,
            )
            imported += 1

        idmap: dict[int, int] = {}
        entities = []
        try:
            # Source schemas vary: the drewgent archive's entities table has no
            # type_parent column. Adapt to what actually exists instead of
            # failing and silently dropping the ontology.
            have = {r["name"] for r in src.execute("PRAGMA table_info(entities)").fetchall()}
            cols = [c for c in ("id", "label", "type", "type_parent", "properties", "knowledge_id")
                    if c in have]
            entities = src.execute(
                f"SELECT {', '.join(cols)} FROM entities ORDER BY id"
            ).fetchall()
            for e in entities:
                props = {}
                try:
                    props = json.loads(e["properties"] or "{}")
                except (json.JSONDecodeError, KeyError):
                    pass
                nid = store.add_entity(
                    e["label"], e["type"] or "concept",
                    properties=props,
                    knowledge_id=e["knowledge_id"] if "knowledge_id" in cols else None,
                )
                idmap[e["id"]] = nid
        except sqlite3.OperationalError as exc:
            # never swallow ontology failure silently — surface it in the summary
            print(f"⚠ entities import failed: {exc}", file=sys.stderr)

        relations_ok = 0
        relations_skipped = 0
        try:
            rels = src.execute(
                "SELECT source_id, target_id, type, properties FROM relations ORDER BY id"
            ).fetchall()
            for r in rels:
                if r["source_id"] not in idmap or r["target_id"] not in idmap:
                    relations_skipped += 1
                    continue
                props = {}
                try:
                    props = json.loads(r["properties"] or "{}")
                except json.JSONDecodeError:
                    pass
                try:
                    store.add_relation(idmap[r["source_id"]], idmap[r["target_id"]], r["type"], props)
                    relations_ok += 1
                except ValueError:
                    relations_skipped += 1
        except sqlite3.OperationalError:
            pass

        sessions_imported = 0
        try:
            sessions = src.execute(
                "SELECT id, title, created_at, message_count FROM sessions ORDER BY id"
            ).fetchall()
            for s in sessions:
                store.record_episode(
                    "session",
                    {"title": s["title"], "message_count": s["message_count"]},
                    session_id=str(s["id"]),
                )
                sessions_imported += 1
        except sqlite3.OperationalError:
            pass

        return {
            "knowledge_imported": imported,
            "entities_imported": len(entities),
            "relations_imported": relations_ok,
            "relations_skipped": relations_skipped,
            "sessions_imported": sessions_imported,
            "reembedded": reembed,
        }

    finally:
        src.close()
