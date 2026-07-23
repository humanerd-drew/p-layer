# P-Layer

**7 layers. 1 memory. Zero chaos.**

Organize your AI agent's memory into governance layers — from immutable rules (P0) to incident retrospectives (P6). Each layer has a contract: who can write, when to query, how to maintain.

```python
pip install p-layers          # repo: p-layer, PyPI: p-layers, import: p_layer
python3 -m p_layer.mcp.server  # starts MCP server with SQLite (zero config)
```

## The 7 Layers

| Layer | Name | Purpose | Query | Write Access |
|-------|------|---------|-------|-------------|
| **P0** | brainstem | Immutable rules | Every session start | system only |
| **P1** | limbic | Identity & persona | Session start + output | human only |
| **P2** | hippocampus | Raw session archive | **Last resort** | append-only |
| **P3** | sensors | Tool integrations | When debugging | system + cron |
| **P4** | cortex | Skills & growth | Skill selection | agent + manual |
| **P5** | ego | **Compiled wiki** | **1st priority** | auto-generated |
| **P6** | prefrontal | Incidents & RCA | During RCA | agent + manual |

## Real-world flow

An AI assistant discovers a bug in the build pipeline:

1. **P6** — Writes an incident report with timeline + root cause
2. **P0** — If the root cause was a rule violation, proposes a P0 amendment
3. **`knowledge_recall`** — When similar symptoms appear weeks later, the MCP server surfaces the incident ranked by confidence + freshness + serendipity
4. **`wiki_compile.py`** — End of day, all incidents + fixes are compiled into P5 wiki pages
5. **Query routing** — Next session, the compiled knowledge is found instantly (P5 first, P2 last)

The same bug never happens twice — not because the agent remembers, but because the governance layer learned.

## MCP Server

7 tools, all included:

| Tool | What it does |
|------|-------------|
| `knowledge_remember` | Store a fact with confidence, TTL, version label |
| `knowledge_recall` | Ranked search — confidence + freshness + 5% serendipity |
| `knowledge_forget` | Soft-delete (supersede, never destroy) |
| `knowledge_update` | Update by ID — old version is superseded, history preserved |
| `knowledge_memory-stats` | Entry counts by layer |
| `knowledge_snapshot-create` | Freeze current state under a version label |
| `knowledge_snapshot-rollback` | Supersede entries created after a snapshot |

### MCP client configuration

**opencode** (`opencode.jsonc`):
```json
{
  "mcp": {
    "p-layer": {
      "type": "local",
      "command": ["python3", "-m", "p_layer.mcp.server"],
      "env": { "KNOWLEDGE_PG_DSN": "{env:KNOWLEDGE_PG_DSN}" },
      "enabled": true
    }
  }
}
```

**Claude Desktop** (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "p-layer": {
      "command": "python3",
      "args": ["-m", "p_layer.mcp.server"],
      "env": { "KNOWLEDGE_PG_DSN": "" }
    }
  }
}
```

## Architecture

```
P0-brainstem (rules) ─────────── governs all layer write permissions
P1-limbic (persona) ──────────── defines agent voice
                                        │
P2-hippocampus (raw data) ──────────────┤
  │                                      │
  ├──→ sessions/ (append-only logs)      │
  ├──→ memories/ (extracted entries)     │
  └──→ knowledge/ (ingested artifacts)   │
                                          ▼
P3-sensors ──→ MCP configs ──→    P_LAYER KNOWLEDGEDB   ←── P4-cortex skill index
                                    (Pg + SQLite)                  │
                                          │                        │
               ┌──────────────────────────┤                        │
               ▼                          ▼                        ▼
     knowledge_recall           P5-ego/wiki/compiled/      P6-prefrontal
     (ranked FTS + vector)      (auto-generated daily)     (incidents + RCA)
                                     │
                               wiki_lint.py
                               (broken link check)
```

### Backend selection

| Variable | Effect |
|----------|--------|
| `KNOWLEDGE_PG_DSN` unset | SQLite mode (`.knowledge/knowledge.db`) |
| `KNOWLEDGE_PG_DSN=dbname=...` | PostgreSQL primary, SQLite fallback |
| `KNOWLEDGE_DB_DIR=/path` | Custom SQLite directory |

## Query Routing (priority order)

When an agent searches for information:

```
1. P5-ego/wiki/compiled/       ← compiled wiki (check FIRST)
2. P5-ego/memory/               ← saved preferences
3. P2-hippocampus/knowledge/    ← raw ingested knowledge
4. P2-hippocampus/memories/     ← raw session memory
5. P2-hippocampus/sessions/     ← raw session logs (LAST resort)
6. KnowledgeDB (SQLite/Pg)      ← cross-cut fallback
```

Steps 1-2 should cover ~80% of queries. Steps 3+ are gaps → next wiki-compile cycle.

## Using p-layers in your project

The `p-layers/` directory contains the canonical governance contracts. Each map to a runtime directory in your project:

```
your-project/
├── p-layers/               ← contract docs (canonical, read-only)
│   ├── P0-brainstem/README.md
│   └── ...
├── P2-hippocampus/         ← runtime data (your sessions, archives)
│   └── sessions/
├── P5-ego/
│   └── wiki/compiled/      ← auto-generated by wiki_compile.py
└── P6-prefrontal/
    └── incidents/          ← your incident reports
```

**Copy** → `cp -r p-layers/ your-project/` (you own them, customize freely)\
**Submodule** → `git submodule add <url>` (stay in sync) \
**Refer** → point your agent's init workflow at `p-layers/P0-brainstem/README.md`

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/ontology_setup.py` | Initialize entity type hierarchy + relation constraints |
| `scripts/seed_knowledge_db.py` | Bootstrap knowledge.db with schema + seeds |
| `scripts/ingest_fact.py` | Insert a single fact from CLI |
| `scripts/ingest_instructions.py` | Batch-ingest .md files into KnowledgeDB |
| `scripts/inference.py` | Transitive closure, backtrace, contradiction detection |
| `scripts/wiki_compile.py` | KnowledgeDB → Markdown wiki pages + INDEX.json |
| `scripts/wiki_lint.py` | Broken link detection, INDEX consistency |

## Ontology Layer

24 entity types across 6 root categories:

```
artifact    → doc, code, project
agent       → persona, tool, script, skill
decision    → pattern, preference
event       → incident, session
knowledge   → concept, paper, reference
meta        → category, _task, fact
```

Relation constraints enforce type safety at insert time:

| Relation | Source → Target |
|----------|----------------|
| `depends_on` | any → tool/script/skill |
| `fixed_by` | incident → pattern/decision |
| `caused` | decision/pattern → incident |
| `led_to` | decision → decision |
| `cites` | paper → paper |
| `contradicts` | decision/pattern → decision/pattern |

## License

MIT


