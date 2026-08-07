"""Minimal MCP stdio server (JSON-RPC 2.0, newline-delimited). Zero dependencies.

Implements just enough of the Model Context Protocol to expose memcore as a
drop-in memory server for any MCP client (opencode, Claude, Cursor, ...):
initialize / notifications/initialized / ping / tools/list / tools/call.

Tools mirror p-layer's MCP surface (remember/recall/forget/update/snapshot)
plus budgeted assemble, with the layer governance enforced in the store.
"""
from __future__ import annotations

import json
import sys

from .store import KNOWLEDGE_TYPES, Store

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "memcore", "version": "0.3.0"}

TOOLS = [
    {
        "name": "remember",
        "description": "Store a fact, decision, preference, or pattern into cross-session memory (default layer P5).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "fact": {"type": "string", "description": "The knowledge to store"},
                "type": {"type": "string", "enum": list(KNOWLEDGE_TYPES), "default": "fact"},
                "source": {"type": "string", "description": "Origin (session, doc, tool)"},
                "layer": {"type": "string", "enum": ["P0", "P1", "P2", "P3", "P4", "P5", "P6"], "default": "P5"},
                "confidence": {"type": "number", "default": 1.0, "description": "0.0-1.0"},
                "ttl_days": {"type": "integer", "description": "Freshness decay horizon in days"},
            },
            "required": ["fact"],
        },
    },
    {
        "name": "recall",
        "description": "Hybrid (semantic + FTS5) search of cross-session memory, ranked by relevance, confidence, and freshness.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords, phrases, or topics"},
                "limit": {"type": "number", "default": 10, "description": "Max results"},
                "serendipity": {"type": "boolean", "default": True, "description": "5% chance to surface one wildcard entry"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "forget",
        "description": "Supersede a memory entry (soft-delete; history preserved, recall stops surfacing it).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "Entry ID"},
                "reason": {"type": "string", "description": "Optional reason"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "update",
        "description": "Update a memory entry — the old version is superseded, a new one is inserted.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "Entry ID"},
                "fact": {"type": "string", "description": "New content"},
                "type": {"type": "string", "enum": list(KNOWLEDGE_TYPES)},
                "confidence": {"type": "number", "description": "0.0-1.0"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "snapshot_create",
        "description": "Freeze the current active entries under a version label.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "version_id": {"type": "string", "description": "e.g. v1, v2.1"},
                "label": {"type": "string", "description": "Human-readable description"},
            },
            "required": ["version_id"],
        },
    },
    {
        "name": "snapshot_rollback",
        "description": "Rollback to a snapshot — supersedes entries created after it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "version_id": {"type": "string", "description": "Snapshot to rollback to"},
            },
            "required": ["version_id"],
        },
    },
    {
        "name": "memory_stats",
        "description": "Store statistics: counts by table, knowledge type, and layer.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "assemble",
        "description": "Budget-bounded context assembly: canonical rules first, then recent knowledge.",
        "inputSchema": {
            "type": "object",
            "properties": {"budget_chars": {"type": "number", "default": 12000}},
        },
    },
    {
        "name": "memory_audit",
        "description": "Recent governance audit entries — every write and every denied write.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "number", "default": 20},
                "denied_only": {"type": "boolean", "default": False},
            },
        },
    },
]


def _result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code: int, message: str):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _call_tool(name: str, arguments: dict, store: Store) -> str:
    if name == "remember":
        fact = arguments.get("fact", "")
        if not fact:
            raise ValueError("remember requires 'fact'")
        kid = store.add_knowledge(
            fact,
            type=arguments.get("type", "fact"),
            source=arguments.get("source"),
            layer=arguments.get("layer", "P5"),
            who="tool:memcore",
            confidence=float(arguments.get("confidence", 1.0)),
            ttl_days=arguments.get("ttl_days"),
        )
        warning = f" (embedding skipped: {store.last_embed_warning})" if store.last_embed_warning else ""
        return f"✓ saved #{kid} ({arguments.get('type', 'fact')}, {arguments.get('layer', 'P5')}){warning}"
    if name == "recall":
        query = arguments.get("query", "")
        results = store.recall(
            query,
            limit=int(arguments.get("limit", 10)),
            serendipity=bool(arguments.get("serendipity", True)),
        )
        if not results:
            return f'Nothing found in memory for "{query}".'
        lines = [
            f"{i + 1}. [{r['type']}][{r['layer']}] {r['content']}"
            + (" (serendipity)" if r.get("_serendipity") else "")
            for i, r in enumerate(results)
        ]
        return f'📚 {len(results)} result(s) for "{query}":\n' + "\n".join(lines)
    if name == "forget":
        kid = int(arguments.get("id", 0))
        ok = store.forget(kid, reason=arguments.get("reason"))
        return f"✓ superseded #{kid}" if ok else f"entry #{kid} not found or already superseded"
    if name == "update":
        kid = int(arguments.get("id", 0))
        new_id = store.update_knowledge(
            kid,
            content=arguments.get("fact"),
            type=arguments.get("type"),
            confidence=float(arguments["confidence"]) if arguments.get("confidence") is not None else None,
        )
        return f"✓ updated #{kid} → #{new_id} (old version superseded, history preserved)"
    if name == "snapshot_create":
        sid = store.snapshot_create(arguments.get("version_id", ""), label=arguments.get("label"))
        return f"✓ snapshot '{arguments.get('version_id')}' created (#{sid})"
    if name == "snapshot_rollback":
        n = store.snapshot_rollback(arguments.get("version_id", ""))
        return f"✓ rolled back to '{arguments.get('version_id')}': {n} entry(ies) superseded"
    if name == "memory_stats":
        return json.dumps(store.stats(), ensure_ascii=False, indent=2)
    if name == "assemble":
        return store.assemble(budget_chars=int(arguments.get("budget_chars", 12000)))
    if name == "memory_audit":
        entries = store.audit_log(
            limit=int(arguments.get("limit", 20)),
            denied_only=bool(arguments.get("denied_only", False)),
        )
        if not entries:
            return "no audit entries"
        lines = [
            f"{'DENIED' if e['denied'] else 'ok    '} {e['action']:<16} #{e['knowledge_id'] or '-'} "
            f"{e['layer'] or '-'} {e['who'] or '-'} :: {e['detail'] or ''}"
            for e in entries
        ]
        return "\n".join(lines)
    raise ValueError(f"unknown tool: {name}")


def handle_message(msg: dict, store: Store) -> dict | None:
    """Process one JSON-RPC message. Returns a response, or None for notifications."""
    method = msg.get("method")
    msg_id = msg.get("id")

    if method == "initialize":
        return _result(
            msg_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        )
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
            text = _call_tool(name, arguments, store)
        except Exception as exc:  # surface tool errors to the client
            return _error(msg_id, -32000, f"{name} failed: {exc}")
        return _result(msg_id, {"content": [{"type": "text", "text": text}]})
    if msg_id is not None:
        return _error(msg_id, -32601, f"method not found: {method}")
    return None


def serve(store: Store | None = None, stdin=None, stdout=None) -> int:
    """Run the MCP stdio loop. Injectable streams for tests."""
    store = store or Store()
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue  # protocol violation; stay alive
        try:
            resp = handle_message(msg, store)
        except Exception:
            resp = None
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0
