"""memcore — production-grade agent memory layer.

Stdlib-only. SQLite + FTS5 + pluggable embeddings. Single-writer, single-user
first, designed so the store can be swapped for Postgres later without touching
the write/read API.

Layers (v2, ported from p-layer's governance design):
  knowledge      — semantic memory (facts, decisions, preferences, patterns)
  episodes       — episodic memory, append-only (sessions, incidents, retros)
  entities/relations — typed ontology (explicit, constraint-validated)
  rules          — canonical rules with precedence (lower priority = higher)
  snapshots      — freeze/rollback active entry sets
  audit_log      — every write and every denied write (governance evidence)
Layer ACLs (P0 system-only ... P6 agent+manual) are enforced in code, not prose.
The eval harness proves the thesis: recall@k vs the drewgent baseline, plus
ACL compliance, in one command (`memcore eval suite.json`).
"""

__version__ = "0.5.0"

from .store import (
    Store,
    KNOWLEDGE_TYPES,
    LAYER_AUTHORITY,
    LAYER_WRITERS,
    RELATION_CONSTRAINTS,
    WriteDenied,
)
from .migrations import migrate, MIGRATIONS
from .embed import Embedder, OllamaEmbedder, HashEmbedder, NoopEmbedder, load_embedder
from .eval import acl_compliance, load_suite, recall_at_k, run_eval

__all__ = [
    "Store",
    "KNOWLEDGE_TYPES",
    "LAYER_AUTHORITY",
    "LAYER_WRITERS",
    "RELATION_CONSTRAINTS",
    "WriteDenied",
    "migrate",
    "MIGRATIONS",
    "Embedder",
    "OllamaEmbedder",
    "HashEmbedder",
    "NoopEmbedder",
    "load_embedder",
    "acl_compliance",
    "load_suite",
    "recall_at_k",
    "run_eval",
    "__version__",
]
