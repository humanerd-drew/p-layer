"""p-layers 0.1.x compatibility layer over the p_layer engine.

Keeps the 0.1.x `KnowledgeDB` API and the `knowledge_*` MCP tool names so
existing integrations upgrade without code changes. The engine underneath is
`p_layer.store.Store`: one schema with checksummed migrations, ACL-enforced
P0-P6 governance, hybrid recall, supersede-not-delete, snapshots, audit.
"""

__version__ = "0.6.1"

from .db import KnowledgeDB, WriteDenied

__all__ = ["KnowledgeDB", "WriteDenied", "__version__"]
