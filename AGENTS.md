# P-Layer Agent Guide

## Identity

This project uses **P-Layer** — a 7-layer governance system for AI agent memory. You are an agent operating within this system. Follow the contracts below.

## Layer Contracts

| Layer | Name | Purpose | You Should |
|-------|------|---------|------------|
| **P0** | brainstem | Immutable rules | Read at session start |
| **P1** | limbic | Personality & voice | Read before generating output |
| **P2** | hippocampus | Raw session archive | Append-only. **Last resort** for queries |
| **P3** | sensors | Tool configs & MCP | Read when debugging integrations |
| **P4** | cortex | Skills & patterns | Read before invoking a skill |
| **P5** | ego | Compiled wiki | **Check first** for any question |
| **P6** | prefrontal | Incidents & RCA | Read during root cause analysis |

Canonical contracts: `p-layers/P{0-6}*/README.md`

## Query Priority

When searching for information:

1. **P5-ego/wiki/compiled/** — compiled wiki (always FIRST)
2. **P5-ego/memory/** — saved preferences
3. **P2-hippocampus/knowledge/** — raw ingested knowledge
4. **P2-hippocampus/memories/** — raw session memory
5. **P2-hippocampus/sessions/** — raw session logs (LAST resort)
6. **KnowledgeDB** (SQLite/Pg) — MCP fallback

~80% of queries resolve at steps 1-2.

## MCP Tools

| Tool | When to Use |
|------|-------------|
| `knowledge_remember` | Persist a fact (with confidence, TTL, version) |
| `knowledge_recall` | Ranked search (confidence + freshness + serendipity) |
| `knowledge_forget` | Supersede an outdated entry |
| `knowledge_update` | Update by ID (history preserved) |
| `knowledge_memory-stats` | Check entry counts by layer |
| `knowledge_snapshot-create` | Freeze state before a risky operation |
| `knowledge_snapshot-rollback` | Revert to a known-good state |

## Ontology

6 root categories, 24 entity types:

```
artifact(tool,code,project)  agent(persona,script,skill)
decision(pattern,preference) event(incident,session)
knowledge(concept,paper,reference)  meta(category,task,fact)
```

Type-safe relations:
- `depends_on` — any → tool/script/skill
- `fixed_by` — incident → pattern/decision
- `caused` — decision/pattern → incident
- `led_to` — decision → decision
- `contradicts` — decision/pattern → decision/pattern

## Workflow: Bug Discovery

1. Write P6 incident report (timeline + root cause)
2. If rule violation, propose P0 amendment
3. Use `knowledge_recall` to check for prior incidents
4. End of day, run `wiki_compile.py` → P5 wiki auto-updated
5. Next session: query routing finds the answer instantly

## Config

**opencode** — add to `opencode.jsonc`:
```json
{"mcp":{"p-layer":{"type":"local","command":["python3","-m","p_layer.mcp.server"],"enabled":true}}}
```

**Claude Desktop** — add to `claude_desktop_config.json`:
```json
{"mcpServers":{"p-layer":{"command":"python3","args":["-m","p_layer.mcp.server"]}}}
```
