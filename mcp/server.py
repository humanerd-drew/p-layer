#!/usr/bin/env python3
"""MCP Agent Memory Server — PgVector-backed with SQLite fallback.

Tools:
  knowledge_remember        — store a fact
  knowledge_recall          — search with confidence ranking + serendipity
  knowledge_forget          — supersede (soft-delete)
  knowledge_update          — update by ID (bumps version)
  knowledge_memory-stats    — stats
  knowledge_snapshot-create — snapshot current entries
  knowledge_snapshot-rollback — rollback to a snapshot
"""

import json
import sys
from datetime import datetime, timezone

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from knowledge_system.core.db import KnowledgeDB
from knowledge_system.core.memory import recall_ranked, _DEFAULT_TTL

server = Server("knowledge-system")


def get_db():
    return KnowledgeDB()


def _has_pg(db) -> bool:
    return db._pg_conn is not None


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="knowledge_remember",
            description="Store a fact, decision, pattern, or incident into persistent memory",
            inputSchema={
                "type": "object",
                "properties": {
                    "fact": {"type": "string", "description": "The content to remember"},
                    "type": {
                        "type": "string",
                        "enum": ["fact", "decision", "pattern", "incident"],
                        "default": "fact",
                    },
                    "confidence": {
                        "type": "number", "default": 1.0,
                        "description": "Confidence score 0.0-1.0",
                    },
                    "ttl_days": {
                        "type": "integer",
                        "description": "Days before deprioritization (default: fact=90, decision=180, pattern=30, incident=365)",
                    },
                    "version_id": {
                        "type": "string", "default": "latest",
                        "description": "Version label for grouping",
                    },
                },
                "required": ["fact"],
            },
        ),
        Tool(
            name="knowledge_recall",
            description="Search persistent memory — ranked by relevance, confidence, and freshness",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "default": 10},
                    "serendipity": {
                        "type": "boolean", "default": True,
                        "description": "Include a wildcard low-ranked entry",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="knowledge_forget",
            description="Supersede a memory entry (soft-delete)",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Entry ID to forget"},
                    "reason": {"type": "string", "description": "Optional reason"},
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="knowledge_memory-stats",
            description="Show memory database statistics",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="knowledge_update",
            description="Update a memory entry — bumps version, old entry is superseded",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Entry ID"},
                    "fact": {"type": "string", "description": "New content"},
                    "type": {
                        "type": "string",
                        "enum": ["fact", "decision", "pattern", "incident"],
                    },
                    "confidence": {"type": "number", "description": "Updated confidence 0.0-1.0"},
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="knowledge_snapshot-create",
            description="Create a snapshot of current entries under a version label",
            inputSchema={
                "type": "object",
                "properties": {
                    "version_id": {"type": "string", "description": "e.g. 'v1', 'v2.1'"},
                    "label": {"type": "string", "description": "Human-readable description"},
                },
                "required": ["version_id"],
            },
        ),
        Tool(
            name="knowledge_snapshot-rollback",
            description="Rollback to a snapshot — marks newer entries as superseded",
            inputSchema={
                "type": "object",
                "properties": {
                    "version_id": {"type": "string", "description": "Snapshot version to rollback to"},
                },
                "required": ["version_id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    db = get_db()

    if name == "knowledge_remember":
        fact = arguments["fact"]
        typ = arguments.get("type", "fact")
        confidence = max(0.0, min(1.0, float(arguments.get("confidence", 1.0))))
        ttl = arguments.get("ttl_days", _DEFAULT_TTL.get(typ, 90))
        version_id = arguments.get("version_id", "latest")

        result = db.insert(
            layer="P6", type=typ, content=fact,
            who="tool:knowledge-system",
            source="knowledge_remember",
        )

        if _has_pg(db) and isinstance(result, dict) and result.get("id"):
            try:
                cur = db._pg_conn.cursor()
                cur.execute(
                    "UPDATE entries SET confidence = %s, ttl_days = %s, version_id = %s WHERE id = %s",
                    (confidence, ttl, version_id, result["id"]),
                )
                db._pg_conn.commit()
            except Exception:
                db._pg_conn.rollback()

        return [TextContent(type="text", text=str(result))]

    elif name == "knowledge_recall":
        query = arguments["query"]
        limit = int(arguments.get("limit", 10))
        serendipity = arguments.get("serendipity", True)

        if not query.strip():
            results = db.search("", layers=["P5", "P6"], limit=limit)
        else:
            results = recall_ranked(db, query, limit=limit, layers=["P5", "P6"], serendipity=serendipity)
        return [TextContent(type="text", text=str(results))]

    elif name == "knowledge_forget":
        mem_id = int(arguments["id"])
        reason = arguments.get("reason", "")

        if _has_pg(db):
            cur = db._pg_conn.cursor()
            cur.execute(
                "UPDATE entries SET superseded_by = id, confidence = GREATEST(confidence, 0.0), "
                "updated_at = NOW() WHERE id = %s",
                (mem_id,),
            )
            db._pg_conn.commit()
            msg = {"superseded": cur.rowcount > 0, "id": mem_id}
            if reason:
                msg["reason"] = reason
            return [TextContent(type="text", text=str(msg))]

        cur = db._connect_sqlite().execute(
            "DELETE FROM memory_fts WHERE rowid = ?", (mem_id,)
        )
        return [TextContent(type="text", text=str({"deleted": cur.rowcount > 0}))]

    elif name == "knowledge_memory-stats":
        counts = db.get_layer_count()
        if _has_pg(db):
            cur = db._pg_conn.cursor()
            cur.execute("SELECT COUNT(*) FROM entries WHERE superseded_by IS NULL")
            active = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM entries WHERE superseded_by IS NOT NULL")
            superseded = cur.fetchone()[0]
        else:
            active = sum(counts.values())
            superseded = 0
        return [TextContent(type="text", text=str({
            "total": sum(counts.values()) if isinstance(counts, dict) else 0,
            "active": active,
            "superseded": superseded,
            "byLayer": counts,
        }))]

    elif name == "knowledge_update":
        mem_id = int(arguments["id"])
        fact = arguments.get("fact")
        typ = arguments.get("type")
        confidence = arguments.get("confidence")

        if _has_pg(db):
            cur = db._pg_conn.cursor()
            if fact:
                cur.execute(
                    "UPDATE entries SET superseded_by = id, updated_at = NOW() WHERE id = %s",
                    (mem_id,),
                )
                new_result = db.insert(
                    layer="P6", type=typ or "fact", content=fact,
                    who="tool:knowledge-system", source="knowledge_update",
                )
                if isinstance(new_result, dict) and new_result.get("id"):
                    cur.execute(
                        "UPDATE entries SET version_id = 'v2' WHERE id = %s",
                        (new_result["id"],),
                    )
                db._pg_conn.commit()
                return [TextContent(type="text", text=str({
                    "updated": True, "superseded_id": mem_id,
                    "new_id": new_result.get("id") if isinstance(new_result, dict) else None,
                }))]

            updates = []
            params = []
            if confidence is not None:
                updates.append("confidence = %s")
                params.append(max(0.0, min(1.0, float(confidence))))
            if typ:
                updates.append("type = %s")
                params.append(typ)
            if updates:
                params.append(mem_id)
                cur.execute(
                    f"UPDATE entries SET {', '.join(updates)}, updated_at = NOW() WHERE id = %s",
                    params,
                )
                db._pg_conn.commit()
                return [TextContent(type="text", text=str({"updated": cur.rowcount > 0}))]

        return [TextContent(type="text", text=str({"updated": False}))]

    elif name == "knowledge_snapshot-create":
        version_id = arguments["version_id"]
        label = arguments.get("label", "")

        if not _has_pg(db):
            return [TextContent(type="text", text=str({"error": "PgVector required for snapshots"}))]

        cur = db._pg_conn.cursor()
        cur.execute("SELECT ARRAY_AGG(id) FROM entries WHERE superseded_by IS NULL")
        entry_ids = cur.fetchone()[0] or []

        cur.execute(
            "INSERT INTO memory_snapshots (version_id, label, entry_ids) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (version_id) DO UPDATE SET label = EXCLUDED.label, "
            "entry_ids = EXCLUDED.entry_ids, created_at = NOW()",
            (version_id, label, entry_ids),
        )
        db._pg_conn.commit()
        return [TextContent(type="text", text=str({
            "version_id": version_id, "entries_snapshot": len(entry_ids),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }))]

    elif name == "knowledge_snapshot-rollback":
        version_id = arguments["version_id"]

        if not _has_pg(db):
            return [TextContent(type="text", text=str({"error": "PgVector required for snapshots"}))]

        cur = db._pg_conn.cursor()
        cur.execute(
            "SELECT entry_ids, created_at FROM memory_snapshots WHERE version_id = %s",
            (version_id,),
        )
        row = cur.fetchone()
        if not row:
            return [TextContent(type="text", text=str({"error": f"Snapshot '{version_id}' not found"}))]

        snapshot_ids, snapshot_at = row
        cur.execute(
            "UPDATE entries SET superseded_by = id, updated_at = NOW() "
            "WHERE id != ALL(%s) AND created_at > %s AND superseded_by IS NULL",
            (snapshot_ids, snapshot_at),
        )
        affected = cur.rowcount
        db._pg_conn.commit()

        return [TextContent(type="text", text=str({
            "version_id": version_id, "entries_rolled_forward": affected,
            "snapshot_entries": len(snapshot_ids),
            "note": "Entries superseded, not deleted. Recall ranking deprioritizes them.",
        }))]

    raise ValueError(f"Unknown tool: {name}")


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
