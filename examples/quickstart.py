#!/usr/bin/env python3
"""p_layer quickstart — API walkthrough: remember, recall, governance, audit.

Run:  python3 examples/quickstart.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from p_layer.embed import NoopEmbedder
from p_layer.store import Store, WriteDenied

db = Store(str(Path(tempfile.mkdtemp()) / "memory.db"), embedder=NoopEmbedder())

# 1. remember — a governance layer is a first-class write parameter
kid = db.add_knowledge(
    "switched to portone v2 for payments",
    type="decision",
    layer="P5",
    who="tool:quickstart",
    source="session: 2026-08-07 payments",
)
db.add_knowledge("client prefers weekly sync calls", type="preference", layer="P6", who="agent")
print(f"remembered #{kid}")

# 2. recall — hybrid, ranked
results = db.recall("portone", limit=5)
print(f"recall 'portone': {len(results)} result(s)")
for r in results:
    print(f"  [{r['score']:.3f}] ({r['type']}, {r['layer']}) {r['content']}")

# 3. governance is enforced in code, and the denial is audited
try:
    db.add_knowledge("rotate the api key weekly", layer="P0", who="agent")
except WriteDenied as exc:
    print(f"denied: {exc}")

# 4. supersede, don't destroy
db.update_knowledge(kid, content="switched to portone v3")
print(f"recall after supersede: {[r['content'] for r in db.recall('portone')]}")

# 5. the audit log is the compliance evidence
print("audit (denied only):")
for e in db.audit_log(denied_only=True):
    print(f"  DENIED {e['action']} {e['layer']} {e['who']} :: {e['detail']}")

print("stats:", db.stats()["active"], "active entries")
