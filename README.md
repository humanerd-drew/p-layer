# P-Layer

**Knowledge governance layers for AI agents.**

A production-tested memory system with PostgreSQL (primary) + SQLite (fallback), built on a P0-P6 governance layer model. Designed for LLM agents that need structured, long-term memory with write permissions, query routing, and automated knowledge compilation.

```
P0-brainstem    Immutable rules (always ON, override all)
P1-limbic       Identity, persona, voice
P2-hippocampus  Raw archive (read-only, append-only)
P3-sensors      Gateways, tool integrations
P4-cortex       Skills index, growth records
P5-ego          Self model, compiled wiki (query priority 1st)
P6-prefrontal   Incidents, retrospectives
```

## Quick Start

```bash
pip install p-layer

# SQLite mode (zero config):
python3 -m knowledge_system.mcp.server

# PostgreSQL mode:
export KNOWLEDGE_PG_DSN="dbname=mykb host=localhost"
python3 -m knowledge_system.mcp.server

# Seed the database:
python3 scripts/seed_knowledge_db.py
python3 scripts/ontology_setup.py
```

## MCP Server (7 tools)

| Tool | Description |
|------|-------------|
| `knowledge_remember` | Store a fact with confidence, TTL, version |
| `knowledge_recall` | Ranked search with confidence+freshness+serendipity |
| `knowledge_forget` | Soft-delete (supersede) |
| `knowledge_update` | Update by ID, bumps version |
| `knowledge_memory-stats` | Entry counts by layer |
| `knowledge_snapshot-create` | Snapshot current entries under a version label |
| `knowledge_snapshot-rollback` | Rollback to snapshot |

## Architecture

```
P2-hippocampus (raw data)
    │
    ▼ ingest scripts + LLM entity extraction
    │
    ├──→ KnowledgeDB (entities, relations, embeddings)
    │       │
    │       ├── knowledge_recall → ranked FTS + vector search
    │       ├── graph tools → entity navigation
    │       └── knowledge_memory-stats → layer counts
    │
    └──→ P5-ego/wiki/compiled/ (daily compile)
            │
            └── wiki_lint → integrity check
```

### Backend Selection

| Variable | Effect |
|----------|--------|
| `KNOWLEDGE_PG_DSN` unset | SQLite mode (`.knowledge/knowledge.db`) |
| `KNOWLEDGE_PG_DSN=dbname=...` | PostgreSQL primary, SQLite fallback |
| `KNOWLEDGE_DB_DIR=/path/to/db` | Custom SQLite path |

### Write Permissions by Layer

| Layer | Who can write |
|-------|--------------|
| P0 | system only |
| P1 | system only |
| P2 | system, gateway, cron |
| P3 | system, gateway, cron |
| P4 | system, cron, agent, manual |
| P5 | system, cron, agent, manual, tool |
| P6 | system, cron, agent, manual, tool |

## Query Routing

When searching for information, follow this strict order:

1. `P5-ego/wiki/compiled/` — compiled wiki pages
2. `P5-ego/memory/` — saved memories / preferences
3. `P2-hippocampus/knowledge/` — raw ingested knowledge
4. `P2-hippocampus/memories/` — raw session memory
5. `P2-hippocampus/sessions/` — raw session logs (last resort)
6. `KnowledgeDB` — cross-cut fallback

## Scripts

| Script | Purpose |
|--------|---------|
| `ontology_setup.py` | Initialize entity type hierarchy + relation constraints |
| `inference.py` | Transitive closure, backtrace, contradiction detection |
| `seed_knowledge_db.py` | Bootstrap knowledge.db with schema + seeds |
| `wiki_compile.py` | KnowledgeDB → Markdown wiki + INDEX.json |
| `wiki_lint.py` | Broken link detection, INDEX consistency |

## Ontology Layer

Entity types (24 types, 6 root categories):

- **artifact** → doc, code, project
- **agent** → persona, tool, script, skill
- **decision** → pattern, preference
- **event** → incident, session
- **knowledge** → concept, paper, reference
- **meta** → category, _task, fact

Relation constraints enforce type safety: `depends_on` only allows tool/script/skill targets, `fixed_by` only source=incident → target=pattern/decision.

## Examples

See `examples/opencode-integration.md` for MCP server setup in opencode.jsonc.

## License

MIT
