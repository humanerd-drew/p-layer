# P0-brainstem — Immutable Rules

## Contract

- **Ownership:** Human only. Agent may propose, human must approve. No autonomous amendment.
- **Persistence:** Immutable once set. Amendment requires deliberate process (propose → review → approve).
- **Query priority:** Checked EVERY session startup before any action.
- **Enforcement:** Layer 0 rules ALWAYS ON. They override `.agent/rules.md`, instructions, and all other P-layers.

## Override Chain

```
P0-brainstem  →  .agent/rules.md  →  .opencode/instructions  →  P1-P6
(highest)                                                        (lowest)
```

## Directory Structure

```
P0-brainstem/
├── README.md         ← This contract
├── rules.md          ← Immutable rule definitions (human-curated)
├── brain/            ← Rule engine, brain state snapshots (optional)
└── verification/     ← Rule compliance checks, enforcement scripts
```

## Knowledge Relations

- **Loaded by:** Session Start Protocol (every session)
- **Fixed by:** P6-incidents may produce P0 rule amendments
- **Referenced by:** All other P-layers — P0 defines their operational boundary

## SOP

### Amendment Flow
1. Proposal drafted
2. Human reviews: "Current rule X causes Y. Proposed change: Z."
3. Approved → update rules.md, archive old rule, record provenance

### Violation Response
1. Detect: verification script or agent self-check
2. Classify: P0 violation (systemic) vs P1-P6 violation (behavioral)
3. Fix: P0 → human intervention required. P1-P6 → auto-remediation + log.
