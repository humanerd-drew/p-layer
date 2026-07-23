# P4-cortex — Skills & Growth

## Contract

- **Ownership:** Agent maintains. Human may curate skill priorities.
- **Persistence:** Mutable — skills added/removed as capabilities evolve.
- **Query priority:** Referenced when selecting a skill for a task.
- **Enforcement:** Skills must have README.md with trigger + provenance to be indexed.

## Directory Structure

```
P4-cortex/
├── README.md         ← This contract
├── skills/           ← Skill index, skill usage records
│   └── SKILL-INDEX.md    ← Master list of all available skills
└── growth/           ← Learning records, refactoring history, milestones
```

## Knowledge Relations

- **Links to:** `skills/` directory (actual skill definitions)
- **Feeds into:** KnowledgeDB — skill usage as entity relations
- **Informs:** P5-ego/SELF_MODEL.md — capability self-awareness

## SOP

### Adding a Skill
1. Create skill with README.md + trigger + provenance
2. Register in SKILL-INDEX.md
3. Link to relevant P3-sensors/gateways/
4. Record: `remember("New skill: {name} for {purpose}")`

### Auditing
- Monthly: scan usage frequency, archive unused skills to P2
- Deprecation: mark in SKILL-INDEX.md, archive, note why
