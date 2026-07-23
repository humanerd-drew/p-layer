# P1-limbic — Identity & Persona

## Contract

- **Ownership:** Human curated. Agent may reference but must not overwrite persona files.
- **Persistence:** Mutable but deliberate. Changes tracked via `drift/` records.
- **Query priority:** Referenced at session start (voice/tone) and during human-facing output.
- **Enforcement:** Writing style guide violations trigger self-correction.

## Directory Structure

```
P1-limbic/
├── README.md               ← This contract
├── persona/
│   ├── SOUL.md                 ← Agent personality, voice, communication style
│   └── writing-style-guide.md  ← Tone, formatting, structure templates
└── drift/                  ← Identity drift tracking records
```

## Knowledge Relations

- **Read by:** Session Start Protocol
- **Referenced by:** Communication Norms workflow
- **Cites:** P0-brainstem rules (layer 0 bounds)
- **Linked to:** @identity/SELF_MODEL.md, @identity/persona/SOUL.md

## SOP

### Onboarding
1. Fill SOUL.md with agent name, tone, style, values
2. Fill writing-style-guide.md with format preferences

### Drift Detection
1. When human corrects tone/voice: record in `drift/`
2. If persistent drift: propose SOUL.md amendment
