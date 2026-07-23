# P2-hippocampus — Raw Archive

## Contract

- **Ownership:** Automatic (session logs, ingest outputs). Human may seed initial knowledge.
- **Persistence:** **Read-only for agent. Append-only for ingestion.** Agent must never modify files in P2.
- **Query priority:** 4th priority — only after P5 (compiled), P5 (memories), P2 (knowledge) are exhausted.

## Query Routing (Knowledge Retrieval Priority)

```
1. P5-ego/wiki/compiled/        ← Compiled wiki (1st priority)
2. P5-ego/memory/               ← Saved memories / preferences (2nd)
3. P2-hippocampus/knowledge/     ← Raw ingested knowledge (3rd)
4. P2-hippocampus/memories/      ← Raw session memory (4th)
5. P2-hippocampus/sessions/      ← Raw session logs (last resort)
6. KnowledgeDB (SQLite/Pg)       ← Cross-cut fallback
```

Step 1-2 hit rate should be ~80%. Steps 3+ gaps get compiled into P5 on next cycle.

## Directory Structure

```
P2-hippocampus/
├── README.md         ← This contract
├── archive/          ← Bulk raw data dumps (read-only)
├── memories/         ← Extracted memory entries (read-only after ingest)
├── knowledge/        ← Raw knowledge artifacts (read-only after ingest)
└── sessions/         ← Session logs (append-only, written by agent)
```

## Knowledge Relations

- **Feeds into:** KnowledgeDB (via ingest scripts)
- **Compiled to:** P5-ego/wiki/compiled/ (via wiki-compile)
- **Referenced by:** graph-rca for incident analysis

## SOP

### Daily Pipeline
1. `scripts/wiki_compile.py` — compile P2 → P5/wiki/compiled/
2. `scripts/wiki_lint.py` — verify wiki integrity, broken links

### Write Rules
- Agent writes to P2: only `sessions/` at session end
- All other P2 writes: via ingest scripts only (never direct)
- Never delete or edit files in P2 (append-only contract)

### Gap Detection
- Query Routing steps 4+ hit → record gap for next wiki-compile
