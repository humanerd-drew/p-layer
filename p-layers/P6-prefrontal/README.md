# P6-prefrontal — Incidents & Retrospectives

## Contract

- **Ownership:** Agent writes after incidents. Human reviews and approves.
- **Persistence:** Append-only. Incidents are immutable once closed.
- **Query priority:** Referenced during RCA and when similar symptoms recur.
- **Enforcement:** Every incident must have `fixed_by` relation in KnowledgeDB pointing to a P0 rule, P1 change, or `.agent/rules.md` update.

## Directory Structure

```
P6-prefrontal/
├── README.md             ← This contract
├── incidents/            ← Incident reports (one per incident)
│   └── TEMPLATE.md           ← Incident report template
└── retrospectives/       ← Periodic retrospectives, pattern analyses
```

## Knowledge Relations

- **Fixed_by →:** P0-brainstem/rules.md, .agent/rules.md, or P1-limbic changes
- **Caused_by ←:** P2-hippocampus/sessions/ — traced to root session
- **Linked to:** KnowledgeDB entity graph via `fixed_by` and `caused` relations

## SOP

### Incident Flow
1. Detect: agent self-check, human report, or monitoring
2. Triage: severity (P0/P1/P2/P3), scope, impact
3. Investigate: trace behavior chain in P2 sessions
4. Fix: apply remediation. P0 rule → escalate to human.
5. Document: write incident report with full timeline
6. Verify: confirm fix, run graph-rca for related unknowns
7. Close: `remember(type="incident")` with `fixed_by` relation

### Retrospective Flow
- Weekly: scan P6/incidents for patterns
- Monthly: write retrospective on top 3 recurring issues
- Quarterly: systemic review — are P0 rules sufficient?

### Template
See `incidents/TEMPLATE.md` for the incident report format.
