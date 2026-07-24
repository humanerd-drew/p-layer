"""Quickstart: pip install p-layers → KnowledgeDB → insert → search."""

from p_layer.core.db import KnowledgeDB

# SQLite mode (zero config, no env vars needed)
db = KnowledgeDB(mode="sqlite")

# Insert a fact
result = db.insert(
    layer="P5",
    type="fact",
    content="P-layer organizes agent memory into 7 governance layers.",
    who="system:quickstart",
)
print(f"Inserted: id={result['id']}")

# Search
results = db.search("governance layers", limit=5)
print(f"Found: {len(results)} results")
for r in results:
    print(f"  [{r['id']}] {r['content'][:80]}...")
