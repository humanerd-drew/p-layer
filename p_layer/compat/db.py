"""KnowledgeDB — p-layers 0.1.x API over the p_layer engine.

Drop-in for existing p-layers code: same class name, same method surface
(insert / search / get_layer_count / graph_query / vector_search /
hybrid_search / close / context manager), same WriteDenied semantics —
enforced by the engine, not by prose.
"""
from __future__ import annotations

import os
from pathlib import Path

from ..embed import NoopEmbedder
from ..store import LAYER_AUTHORITY, Store, WriteDenied  # re-export WriteDenied

DEFAULT_DB_DIR = os.environ.get("KNOWLEDGE_DB_DIR") or str(Path.cwd() / ".knowledge")


class KnowledgeDB:
    """Drop-in replacement for p-layers 0.1.x KnowledgeDB (SQLite mode)."""

    def __init__(self, mode: str = "auto", dsn: str | None = None, db_dir: str | None = None):
        if mode not in ("auto", "sqlite"):
            raise NotImplementedError(
                "p-layers 1.0 is SQLite-only; the PostgreSQL path is deferred "
                "to the multi-agent/SMB phase. Use mode='auto' or 'sqlite'."
            )
        self.mode = "sqlite"
        self.db_dir = Path(db_dir or DEFAULT_DB_DIR)
        self._store = Store(str(self.db_dir / "knowledge.db"), embedder=NoopEmbedder())

    # ── 0.1.x surface ─────────────────────────────────────────
    @property
    def has_pg(self) -> bool:
        return False

    @property
    def available(self) -> bool:
        return True

    def set_mode(self, mode: str):
        if mode not in ("auto", "sqlite"):
            raise NotImplementedError(
                "p-layers 1.0 is SQLite-only; the PostgreSQL path is deferred "
                "to the multi-agent/SMB phase. Use mode='auto' or 'sqlite'."
            )
        self.mode = "sqlite"

    def insert(self, layer: str, type: str, content: str, who: str = "system",
               source: str = "", source_path: str = "", embedding: list | None = None,
               created_at: str | None = None) -> dict:
        """Store an entry. Layer ACLs are enforced by the engine (WriteDenied)."""
        kid = self._store.add_knowledge(
            content,
            type=type,
            source=source or None,
            created_at=created_at,
            layer=layer,
            who=who,
        )
        row = self._store.db.execute(
            "SELECT created_at FROM knowledge WHERE id = ?", (kid,)
        ).fetchone()
        return {
            "id": kid,
            "who": who,
            "type": type,
            "layer": layer,
            "authority": LAYER_AUTHORITY.get(layer),
            "created_at": row["created_at"] if row else created_at,
        }

    def search(self, query: str, layers: list | None = None, limit: int = 20,
               who: str | None = None) -> list:
        results = self._store.recall(query, limit=limit, serendipity=False)
        if layers is not None:
            results = [r for r in results if r["layer"] in layers]
        if who is not None:
            results = [r for r in results if r["who"] == who]
        return results

    def get_layer_count(self) -> dict:
        """Real per-layer counts (0.1.x SQLite returned a placeholder)."""
        return self._store.stats()["by_layer"]

    def graph_query(self, query: str, depth: int = 2) -> dict:
        """0.1.x graph shape: {nodes: [...], edges: [...]}."""
        explored = self._store.graph_explore(query, depth=depth)
        nodes = []
        edges = []
        for e in explored["entities"]:
            nodes.append({"id": e["id"], "label": e["label"], "type": e["type"]})
            for n in e["neighbors"]:
                edges.append({
                    "source": e["label"],
                    "target": n["label"],
                    "type": n["relation"],
                })
        return {"nodes": nodes, "edges": edges}

    def vector_search(self, vector: list, limit: int = 20) -> list:
        # 0.1.x returned [] in SQLite mode; keep the contract.
        return []

    def hybrid_search(self, query: str, vector: list | None = None, limit: int = 20) -> list:
        return self.search(query, limit=limit)

    def close(self) -> None:
        self._store.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()
