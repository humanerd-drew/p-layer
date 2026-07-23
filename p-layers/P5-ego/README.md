# P5-ego — Self Model & Compiled Knowledge

## Contract

- **Ownership:** SELF_MODEL.md = human curated. wiki/compiled/ = auto-generated from P2.
- **Persistence:** Self-model is curated. Compiled wiki is regenerated daily.
- **Query priority:** **1st priority** — Always check compiled wiki before raw P2.
- **Enforcement:** wiki/compiled/ must not be manually edited (auto-generated).

## Query Routing Priority

```
1. P5-ego/wiki/compiled/   ← Check FIRST
2. P5-ego/memory/          ← Check SECOND
3. P2-hippocampus/         ← Fallback
```

## Directory Structure

```
P5-ego/
├── README.md             ← This contract
├── SELF_MODEL.md         ← Core identity, capabilities, limitations
├── wiki/
│   ├── compiled/             ← Daily-compiled knowledge (read-only, auto-generated)
│   │   ├── INDEX.json            ← Page index for fast lookup
│   │   └── *.md                 ← Compiled wiki pages
│   └── lint-report.md           ← Latest lint output
└── memory/               ← Important memories, preferences, decisions
```

## Knowledge Relations

- **Source:** P2-hippocampus → scripts/wiki_compile.py → P5/wiki/compiled/
- **Feeds into:** KnowledgeDB — compiled wiki is queried first
- **Links to:** @identity/SELF_MODEL.md — agent self-awareness
- **Informed by:** P4-cortex/growth/ — capability tracking

## SOP

### Daily
1. `scripts/wiki_compile.py`: P2 → P5/wiki/compiled/
2. `scripts/wiki_lint.py`: validate compiled wiki integrity

### Self-Model Updates
- After significant growth/capability change: update SELF_MODEL.md
- Provenance: record trigger in session log
- Never auto-generate self-model — human review required

### Gap Recovery
- When query routing hits P2: flag for next wiki-compile
- Systemic gaps → P6-prefrontal incident
