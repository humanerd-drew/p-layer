# Integrating p-layer with opencode

Add the MCP server to your `opencode.jsonc`:

```json
{
  "mcp": {
    "p-layer": {
      "type": "local",
      "command": ["python3", "-m", "knowledge_system.mcp.server"],
      "env": {
        "KNOWLEDGE_PG_DSN": "{env:KNOWLEDGE_PG_DSN}",
        "KNOWLEDGE_DB_DIR": "{env:HOME}/.agent/memory"
      },
      "enabled": true
    }
  }
}
```

## Tool Mapping

| opencode tool | p-layer MCP tool |
|--------------|-----------------|
| `agent-memory_remember` | `knowledge_remember` |
| `agent-memory_recall` | `knowledge_recall` |
| `agent-memory_forget` | `knowledge_forget` |
| `agent-memory_update` | `knowledge_update` |
| `agent-memory_memory-stats` | `knowledge_memory-stats` |
| `agent-memory_snapshot-create` | `knowledge_snapshot-create` |
| `agent-memory_snapshot-rollback` | `knowledge_snapshot-rollback` |

## Session Start Protocol Integration

In `.agent/workflow/init.md`, step 4 should include:

```
- `read p-layers/P0-brainstem/README.md` — P-layer contract (immutable rules)
- `read p-layers/P2-hippocampus/README.md` — query routing contract
- `read p-layer/README.md` — knowledge system overview
```

## Directory Setup

```
project-root/
├── p-layers/           ← symlink or copy from p-layer/p-layers/
│   ├── P0-brainstem/README.md
│   ├── P2-hippocampus/README.md
│   └── ...
├── P2-hippocampus/     ← runtime data
│   └── sessions/
├── P5-ego/
│   └── wiki/compiled/
└── ...
```
