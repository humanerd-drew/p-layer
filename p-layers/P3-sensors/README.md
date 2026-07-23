# P3-sensors — Gateways & Tool Integration

## Contract

- **Ownership:** Agent configures. Human approves new gateways.
- **Persistence:** Mutable — tool configs change as stack evolves.
- **Query priority:** Referenced when setting up or debugging tool integrations.
- **Enforcement:** Gateway configs must pass validation before activation.

## Directory Structure

```
P3-sensors/
├── README.md         ← This contract
├── gateways/         ← MCP server configs, tool wrappers, API clients
└── integrations/     ← External service integration docs and schemas
```

## Knowledge Relations

- **Referenced by:** opencode.jsonc (MCP server definitions)
- **Linked to:** P4-cortex/skills/ — skills consume sensor gateways
- **Feeds into:** P2-hippocampus/sessions/ — sensor outputs are logged

## SOP

### Adding a Gateway
1. Document integration in `gateways/`
2. Add MCP server entry to `opencode.jsonc`
3. Test with minimal call
4. Record in P2-hippocampus/sessions/

### Deprecation
1. Disable in config (don't delete)
2. Archive gateway doc to P2-hippocampus/archive/
3. Note in P6-prefrontal/retrospectives/ why it was removed
