# memcore

**Governed memory for AI agents.** A stdlib-only Python memory layer — SQLite + FTS5 + pluggable embeddings — with P0-P6 layer governance enforced in code, not prose.

```
7 layers. 1 memory. Every write audited.
```

[한국어](./README.ko.md)

## Why this exists

The P0-P6 "brain layer" memory idea (drewgent, p-layer) is sound, but the reference implementations carry the same core defects:

| Defect | drewgent / p-layer | memcore |
|---|---|---|
| Schema management | `CREATE TABLE IF NOT EXISTS` everywhere, no versioning | Forward-only, checksummed migrations (`schema_migrations`) |
| Search index | External-content FTS5 + triggers (fragile; p-layer's `forget` breaks it) | Standalone FTS5, no trigger coupling |
| Dual backends | Two parallel implementations that drift (TS + Python; SQLite + Pg with silent feature loss) | One implementation, one schema |
| Governance | A table in the README ("P0 overrides everything") | **Enforced in code**: layer ACLs raise `WriteDenied` |
| "Remember" tool | Hardcodes layer=P6, bypassing its own governance | Layer is a first-class write parameter, ACL-checked |

This repo is the production-grade rebuild: the governance ideas ported from p-layer, the schema discipline the originals lacked, and an eval harness that proves governance improves retrieval.

## What it does

| Feature | What |
|---|---|
| **P0-P6 layer ACLs** | Who may write to each layer is enforced in code (`P0` system-only … `P6` agent+manual). Denied writes are audited. |
| **Hybrid recall** | FTS5 + semantic (Ollama or pluggable), RRF fusion, ranked by confidence × freshness, type-diversified, superseded excluded. |
| **Supersede-not-delete** | `forget`/`update` supersede entries; history is preserved and recall stops surfacing them. |
| **Snapshots** | Freeze active entries under a version label; rollback supersedes everything after the snapshot. |
| **Audit log** | Every write *and every denied write* is recorded — the compliance evidence. |
| **Contradiction scan** | Heuristic scan (no LLM): conflicting rule priorities, cross-layer duplicates. |
| **P5 wiki compile** | Offline compile of active memory into per-layer markdown with provenance + INDEX. |
| **MCP server** | 9 tools (`remember`, `recall`, `forget`, `update`, `snapshot_*`, `memory_stats`, `memory_audit`, `assemble`) — zero-dependency stdio implementation. |
| **Import tool** | `import-drewgent` migrates an existing drewgent `knowledge.db` (schema re-validated, re-embedded). |

## Proof: governance improves retrieval

Same data, two engines, one command (`memcore eval suite.json`):

```
recall@k (same data, two engines):
  drewgent baseline : 0.667 (2/3)      ← naive FTS OR-join, insertion order
  memcore           : 1.000 (3/3)      ← confidence/freshness-ranked
  delta             : +0.333
ACL compliance: 100.0% (30/30) enforcement cases correct
```

The baseline can't move — it has no metadata. memcore turns governance metadata (confidence, layer, supersession) into retrieval quality, and the ACL suite proves the governance is real: every (layer, who) combination is allowed or denied exactly as specified.

## Quick start

```bash
# no dependencies — stdlib only (Python >= 3.9)
export MEMCORE_EMBED=hash   # offline fallback; ollama is the default
export MEMCORE_DB=~/.memcore/memory.db

python3 -m memcore init
python3 -m memcore remember "switched to portone v2 for payments" --type decision --layer P5
python3 -m memcore recall "portone"
python3 -m memcore assemble --budget 12000     # rules first, then recent knowledge
```

Python API:

```python
from memcore.store import Store, WriteDenied

db = Store()
db.add_knowledge("client prefers weekly sync", type="preference", layer="P6", who="agent")
print(db.recall("weekly sync", limit=5))
try:
    db.add_knowledge("secret", layer="P0", who="agent")   # P0 is system-only
except WriteDenied:
    pass
print(db.audit_log(denied_only=True))                     # the denial is on record
```

MCP (any client — opencode, Claude Desktop, Cursor):

```json
{
  "mcp": {
    "memcore": {
      "type": "local",
      "command": ["python3", "-m", "memcore", "serve"],
      "env": { "MEMCORE_DB": "~/.memcore/memory.db" }
    }
  }
}
```

## Architecture

```
                ┌─────────────────────────────────────────────┐
  rules (P0-P1) │  knowledge (P2-P6)   episodes    entities/   │
  precedence-   │  FTS5 + embeddings   append-only  relations  │
  ordered       │  + confidence/TTL    (sessions,   (typed,    │
                │  + superseded_by     incidents)   validated) │
                └─────────────────────────────────────────────┘
                    SQLite (WAL, FK on) — one schema, migrations v1→v3
                                    │
        ┌───────────────┬───────────┼──────────────┬────────────┐
   recall (hybrid)  assemble (budget)  audit_log  contradictions  compile_wiki
   RRF+conf+fresh   rules→recent       every write   heuristic     P5 wiki
```

Tables: `knowledge` · `knowledge_fts` · `embeddings` (versioned) · `episodes` · `entities` · `relations` (constraint-validated) · `rules` · `snapshots` · `audit_log` · `schema_migrations`.

## Governance model

| Layer | Purpose | Who may write |
|---|---|---|
| P0 | Immutable rules | system only |
| P1 | Identity & persona | system only |
| P2 | Raw session archive | system, gateway, cron |
| P3 | Tool integrations | system, gateway, cron |
| P4 | Skills & growth | system, cron, agent, manual |
| P5 | Compiled knowledge | system, cron, agent, manual, tool |
| P6 | Incidents & RCA | system, cron, agent, manual, tool |

Precedence is data, not prose: lower priority/higher authority wins, and `assemble()` emits rules in precedence order under a token budget.

## Development

```bash
python3 -m unittest discover -s tests -v   # 59 tests, no deps, no network
```

## Examples

- `examples/quickstart.py` — API walkthrough
- `examples/demo_import_eval.sh` — the full proof pipeline: drewgent fixture → import → eval before/after governance → audit → contradictions → wiki
- `examples/suite.example.json` — eval suite format

## Credits

Built as a production-grade rebuild of ideas from:

- [opencode-drewgent](https://github.com/humanerd-drew/opencode-drewgent) — P0-P6 vault concept, provenance convention
- [p-layer](https://github.com/humanerd-drew/p-layer) — layer authority/ACL design, supersede-not-delete, confidence/TTL ranking, snapshots
- [Gajae-Code](https://github.com/Yeachan-Heo/gajae-code) — agent orchestration conventions

The critique that motivated this repo is documented in the README above; the credits are where the good ideas came from.

## License

MIT
