"""p_layer — production-grade agent memory layer.

Stdlib-only. SQLite + FTS5 + pluggable embeddings. Single-writer, single-user
first; the same write/read API is also implemented by p_layer.pgstore.PgStore
(PostgreSQL, parity-tested) for shared multi-agent use.

Layers (v2, ported from p-layer's governance design):
  knowledge      — semantic memory (facts, decisions, preferences, patterns)
  episodes       — episodic memory, append-only (sessions, incidents, retros)
  entities/relations — typed ontology (explicit, constraint-validated)
  rules          — canonical rules with precedence (lower priority = higher)
  snapshots      — freeze/rollback active entry sets
  audit_log      — every write and every denied write (governance evidence)
Layer ACLs (P0 system-only ... P6 agent+manual) are enforced in code, not prose.
The eval harness proves the thesis: recall@k vs the drewgent baseline, plus
ACL compliance, in one command (`p_layer eval suite.json`).
"""

__version__ = "0.6.1"

from .store import (
    Store,
    KNOWLEDGE_TYPES,
    LAYER_AUTHORITY,
    LAYER_WRITERS,
    RELATION_CONSTRAINTS,
    RerankConfig,
    DEFAULT_RERANK,
    WriteDenied,
    rrf_fuse,
)
from .migrations import migrate, MIGRATIONS
from .embed import Embedder, OllamaEmbedder, HashEmbedder, NoopEmbedder, load_embedder
from .eval import acl_compliance, load_suite, recall_at_k, run_eval
from .pgstore import PgStore

__all__ = [
    "Store",
    "PgStore",
    "RerankConfig",
    "DEFAULT_RERANK",
    "rrf_fuse",
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
