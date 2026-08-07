"""p-layers 1.0 MCP server — knowledge_* tool names over the p_layer engine.

Drop-in for 0.1.x MCP clients (opencode, Claude Desktop): same tool names
(knowledge_remember, knowledge_recall, knowledge_forget, knowledge_update,
knowledge_memory-stats, knowledge_snapshot-create, knowledge_snapshot-rollback,
knowledge_ontology-status), same stdio JSON-RPC protocol, zero deps.
"""
from __future__ import annotations

import json
import sys

from .db import KnowledgeDB

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "p-layers", "version": "0.6.0"}

TOOLS = [
    {
        "name": "knowledge_remember",
        "description": "Store a fact, decision, preference, or pattern into governed memory (default layer P5).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "fact": {"type": "string"},
                "type": {"type": "string", "enum": ["fact", "decision", "pattern", "preference"], "default": "fact"},
                "layer": {"type": "string", "enum": ["P0", "P1", "P2", "P3", "P4", "P5", "P6"], "default": "P5"},
                "confidence": {"type": "number", "default": 1.0},
                "ttl_days": {"type": "integer"},
            },
            "required": ["fact"],
        },
    },
    {
        "name": "knowledge_recall",
        "description": "Search governed memory — hybrid FTS5 + semantic, confidence/freshness ranked.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
                "serendipity": {"type": "boolean", "default": True},
            },
            "required": ["query"],
        },
    },
    {
        "name": "knowledge_forget",
        "description": "Supersede a memory entry (soft-delete, history preserved).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "reason": {"type": "string"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "knowledge_update",
        "description": "Update a memory entry — old version superseded, new one inserted.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "fact": {"type": "string"},
                "type": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "knowledge_memory-stats",
        "description": "Store statistics: counts by table, layer, and type.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "knowledge_snapshot-create",
        "description": "Freeze the active entry set under a version label.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "version_id": {"type": "string"},
                "label": {"type": "string"},
            },
            "required": ["version_id"],
        },
    },
    {
        "name": "knowledge_snapshot-rollback",
        "description": "Rollback to a snapshot — supersedes entries created after it.",
        "inputSchema": {
            "type": "object",
            "properties": {"version_id": {"type": "string"}},
            "required": ["version_id"],
        },
    },
    {
        "name": "knowledge_ontology-status",
        "description": "Ontology health: entity/relation counts and contradiction findings.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code: int, message: str):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _call_tool(name: str, arguments: dict, db: KnowledgeDB) -> str:
    if name == "knowledge_remember":
        fact = arguments.get("fact", "")
        if not fact:
            raise ValueError("knowledge_remember requires 'fact'")
        result = db.insert(
            layer=arguments.get("layer", "P5"),
            type=arguments.get("type", "fact"),
            content=fact,
            who="tool:p-layers",
        )
        return json.dumps(result, ensure_ascii=False)
    if name == "knowledge_recall":
        query = arguments.get("query", "")
        results = db._store.recall(
            query,
            limit=int(arguments.get("limit", 10)),
            serendipity=bool(arguments.get("serendipity", True)),
        )
        if not results:
            return json.dumps([], ensure_ascii=False)
        return json.dumps(results, ensure_ascii=False)
    if name == "knowledge_forget":
        kid = int(arguments.get("id", 0))
        ok = db._store.forget(kid, reason=arguments.get("reason"))
        return json.dumps({"superseded": ok, "id": kid}, ensure_ascii=False)
    if name == "knowledge_update":
        kid = int(arguments.get("id", 0))
        new_id = db._store.update_knowledge(
            kid,
            content=arguments.get("fact"),
            type=arguments.get("type"),
            confidence=float(arguments["confidence"]) if arguments.get("confidence") is not None else None,
        )
        return json.dumps({"updated": True, "superseded_id": kid, "new_id": new_id}, ensure_ascii=False)
    if name == "knowledge_memory-stats":
        return json.dumps(db._store.stats(), ensure_ascii=False)
    if name == "knowledge_snapshot-create":
        version_id = arguments.get("version_id", "")
        db._store.snapshot_create(version_id, label=arguments.get("label"))
        return json.dumps({"created": True, "version_id": version_id}, ensure_ascii=False)
    if name == "knowledge_snapshot-rollback":
        version_id = arguments.get("version_id", "")
        n = db._store.snapshot_rollback(version_id)
        return json.dumps({"rolled_back": n, "version_id": version_id}, ensure_ascii=False)
    if name == "knowledge_ontology-status":
        stats = db._store.stats()
        return json.dumps({
            "entities": stats["entities"],
            "relations": stats["relations"],
            "contradictions": len(db._store.contradictions()),
        }, ensure_ascii=False)
    raise ValueError(f"unknown tool: {name}")


def handle_message(msg: dict, db: KnowledgeDB) -> dict | None:
    """Process one JSON-RPC message. Returns a response, or None for notifications."""
    method = msg.get("method")
    msg_id = msg.get("id")

    if method == "initialize":
        return _result(msg_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        params = msg.get("params", {})
        name = params.get("name", "")
        arguments = params.get("arguments", {}) or {}
        try:
            text = _call_tool(name, arguments, db)
        except Exception as exc:
            return _error(msg_id, -32000, f"{name} failed: {exc}")
        return _result(msg_id, {"content": [{"type": "text", "text": text}]})
    if msg_id is not None:
        return _error(msg_id, -32601, f"method not found: {method}")
    return None


def serve(db: KnowledgeDB | None = None, stdin=None, stdout=None) -> int:
    db = db or KnowledgeDB()
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle_message(msg, db)
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(serve())
