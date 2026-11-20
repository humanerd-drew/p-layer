"""Read-only, zero-dependency 2D/3D interactive Memory Graph UI & SSE server for p_layer.

Exposes ``PLayerGraphAdapter`` and ``serve_ui()`` used by ``p-layer ui``.
Opens the SQLite store strictly read-only (``?mode=ro`` + ``PRAGMA query_only=ON``)
and binds to loopback by default unless ``allow_remote=True`` is explicitly requested.
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import json
import re
import sqlite3
import time
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from .store import LAYER_AUTHORITY

DEFAULT_UI_HOST = "127.0.0.1"
DEFAULT_UI_PORT = 8765
DEFAULT_LIMIT = 800
MAX_LIMIT = 2500
SSE_POLL_SECONDS = 1.5

GROUP_META: List[Tuple[str, str, str]] = [
    ("P0", "P0 Rules", "#ffc533"),
    ("P1", "P1 Identity", "#ff7a7a"),
    ("P2", "P2 Sessions", "#f59e0b"),
    ("P3", "P3 Tools", "#2dd4bf"),
    ("P4", "P4 Skills", "#a78bfa"),
    ("P5", "P5 Compiled", "#57c1ff"),
    ("P6", "P6 Incidents", "#59d499"),
    ("entity", "Ontology Entity", "#38bdf8"),
    ("rule", "Rule", "#fb7185"),
    ("episode", "Episode", "#64748b"),
    ("snapshot", "Snapshot", "#94a3b8"),
]
GROUP_COLOR_MAP: Dict[str, str] = {key: color for key, _, color in GROUP_META}


def is_loopback_host(host: str) -> bool:
    """Return whether ``host`` resolves to the local loopback interface."""
    cleaned = str(host or "").strip().lower()
    if cleaned in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(cleaned).is_loopback
    except ValueError:
        return False


def connect_readonly(db_path: Path | str) -> sqlite3.Connection:
    """Open ``db_path`` strictly read-only with busy timeout and query_only."""
    raw = str(db_path)
    if raw == ":memory:":
        raise ValueError("connect_readonly requires a filesystem SQLite path")
    resolved = Path(db_path).expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"database not found: {resolved}")
    uri = resolved.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA query_only = ON")
    return conn


def _table_names(conn: sqlite3.Connection) -> Set[str]:
    return {
        str(r[0])
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
    }


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _short_label(text: Optional[str], fallback: str, max_len: int = 44) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if not cleaned:
        return fallback
    cleaned = re.sub(r"^#+\s*", "", cleaned)
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 1].rstrip() + "…"


def _preview_text(text: Optional[str], max_len: int = 160) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 1].rstrip() + "…"


def _fts_query_tokens(query: str) -> Optional[str]:
    tokens = re.findall(r"[A-Za-z0-9_]+", query)
    if not tokens:
        return None
    return " OR ".join(f'"{t}"' for t in tokens[:6])


class PLayerGraphAdapter:
    """Read-only graph projection over a p_layer SQLite store."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)

    def _open(self) -> sqlite3.Connection:
        return connect_readonly(self.db_path)

    @staticmethod
    def _collect_stats(conn: sqlite3.Connection, tables: Set[str]) -> Dict[str, int]:
        def _count(tbl: str, where: str = "") -> int:
            if tbl not in tables:
                return 0
            q = f"SELECT COUNT(*) FROM {tbl}" + (f" WHERE {where}" if where else "")
            return int(conn.execute(q).fetchone()[0])

        return {
            "total_knowledge": _count("knowledge"),
            "active_knowledge": _count("knowledge", "superseded_by IS NULL"),
            "total_entities": _count("entities"),
            "total_relations": _count("relations"),
            "total_rules": _count("rules", "enabled = 1"),
            "total_episodes": _count("episodes"),
            "denied_writes": _count("audit_log", "denied = 1"),
        }

    @staticmethod
    def _read_audit(
        conn: sqlite3.Connection, tables: Set[str], limit: int = 25
    ) -> List[Dict[str, Any]]:
        if "audit_log" not in tables:
            return []
        rows = conn.execute(
            "SELECT id, action, knowledge_id, layer, who, detail, denied, created_at "
            "FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "action": r["action"],
                "target_id": f"k:{r['knowledge_id']}" if r["knowledge_id"] else (r["layer"] or ""),
                "knowledge_id": r["knowledge_id"],
                "layer": r["layer"] or "",
                "caller": r["who"] or "system",
                "denied": bool(r["denied"]),
                "detail": r["detail"] or "",
                "created_at": r["created_at"] or "",
            }
            for r in rows
        ]

    @staticmethod
    def _knowledge_to_node(row: sqlite3.Row) -> Dict[str, Any]:
        layer = str(row["layer"] or "P5")
        group = layer if layer in GROUP_COLOR_MAP else "P5"
        auth = float(LAYER_AUTHORITY.get(layer, 30)) / 100.0
        conf = float(row["confidence"] if row["confidence"] is not None else 1.0)
        superseded = row["superseded_by"] is not None
        weight = round((0.85 + auth * 0.95) * (0.7 if superseded else 1.0), 3)
        return {
            "id": f"k:{row['id']}",
            "raw_id": str(row["id"]),
            "source_table": "knowledge",
            "label": _short_label(row["content"], f"#{row['id']} ({layer})", 42),
            "preview": _preview_text(row["content"], 180),
            "kind": f"{layer} {row['type']}",
            "group": group,
            "weight": weight,
            "confidence": round(conf, 3),
            "created_at": row["created_at"] or "",
            "pinned": layer in ("P0", "P1"),
            "meta": {
                "layer": layer,
                "type": row["type"],
                "who": row["who"],
                "source": row["source"] or "",
                "session_id": row["session_id"] or "",
                "superseded_by": row["superseded_by"],
            },
        }

    @staticmethod
    def _entity_to_node(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": f"ent:{row['id']}",
            "raw_id": str(row["id"]),
            "source_table": "entities",
            "label": _short_label(row["label"], f"entity:{row['id']}", 42),
            "preview": f"[{row['type']}] {row['properties'] or ''}".strip(),
            "kind": f"entity:{row['type']}",
            "group": "entity",
            "weight": 1.35,
            "confidence": 1.0,
            "created_at": row["created_at"] or "",
            "pinned": False,
            "meta": {
                "entity_type": row["type"],
                "type_parent": row["type_parent"] or "",
                "knowledge_id": row["knowledge_id"],
            },
        }

    @staticmethod
    def _rule_to_node(row: sqlite3.Row) -> Dict[str, Any]:
        layer = str(row["layer"] or "P0")
        return {
            "id": f"rule:{row['id']}",
            "raw_id": str(row["id"]),
            "source_table": "rules",
            "label": _short_label(row["text"], f"rule:{row['id']}", 42),
            "preview": _preview_text(row["text"], 180),
            "kind": f"rule ({layer})",
            "group": "rule",
            "weight": 1.65,
            "confidence": 1.0,
            "created_at": row["created_at"] or "",
            "pinned": layer in ("P0", "P1"),
            "meta": {
                "layer": layer,
                "priority": row["priority"],
                "scope": row["scope"] or "global",
            },
        }

    def build_graph(
        self,
        mode: str = "curated",
        query: Optional[str] = None,
        kinds: Optional[Iterable[str]] = None,
        limit: int = DEFAULT_LIMIT,
    ) -> Dict[str, Any]:
        """Build a normalized 2D/3D graph JSON payload from the p_layer store."""
        norm_mode = mode if mode in ("curated", "ontology", "all") else "curated"
        bounded_limit = max(20, min(int(limit), MAX_LIMIT))
        cleaned_query = (query or "").strip()
        kind_filter: Optional[Set[str]] = None
        if kinds:
            parsed = {k.strip() for k in kinds if str(k).strip()}
            if parsed:
                kind_filter = parsed

        conn = self._open()
        try:
            tables = _table_names(conn)
            stats = self._collect_stats(conn, tables)
            audit = self._read_audit(conn, tables, limit=25)

            nodes_by_id: Dict[str, Dict[str, Any]] = {}
            edges_list: List[Dict[str, Any]] = []
            edge_seen: Set[Tuple[str, str, str]] = set()

            def add_edge(src: str, dst: str, rel: str, weight: float = 1.0) -> None:
                if src == dst or src not in nodes_by_id or dst not in nodes_by_id:
                    return
                key = (src, dst, rel)
                if key in edge_seen:
                    return
                edge_seen.add(key)
                edges_list.append(
                    {
                        "id": f"e:{len(edges_list) + 1}",
                        "source": src,
                        "target": dst,
                        "rel": rel,
                        "weight": round(float(weight), 3),
                    }
                )

            if cleaned_query:
                self._populate_search(conn, tables, cleaned_query, bounded_limit, nodes_by_id, add_edge)
            elif norm_mode == "ontology":
                self._populate_ontology(conn, tables, bounded_limit, nodes_by_id, add_edge)
            else:
                include_extra = norm_mode == "all"
                self._populate_curated(
                    conn, tables, bounded_limit, include_extra, nodes_by_id, add_edge
                )

            if kind_filter is not None:
                kept = {nid for nid, n in nodes_by_id.items() if n["group"] in kind_filter}
                nodes_by_id = {nid: nodes_by_id[nid] for nid in kept}
                edges_list = [
                    e for e in edges_list if e["source"] in kept and e["target"] in kept
                ]

            deg = Counter()
            for e in edges_list:
                deg[e["source"]] += 1
                deg[e["target"]] += 1
            for nid, node in nodes_by_id.items():
                node["degree"] = int(deg.get(nid, 0))

            nodes_out = sorted(
                nodes_by_id.values(), key=lambda n: (-n["degree"], -n["weight"], n["id"])
            )
            group_counts = Counter(n["group"] for n in nodes_out)
            groups_out = [
                {
                    "key": key,
                    "label": label,
                    "color": color,
                    "count": int(group_counts.get(key, 0)),
                }
                for key, label, color in GROUP_META
            ]

            layer_slots = self._build_layer_summary(conn, tables)
            stats["rendered_nodes"] = len(nodes_out)
            stats["rendered_edges"] = len(edges_list)

            return {
                "nodes": nodes_out,
                "edges": edges_list,
                "groups": groups_out,
                "slots": layer_slots,
                "audit": audit,
                "stats": stats,
                "meta": {
                    "mode": norm_mode,
                    "query": cleaned_query,
                    "flavor": "p_layer",
                    "db_name": self.db_path.name,
                    "readonly": True,
                },
            }
        finally:
            conn.close()

    @staticmethod
    def _build_layer_summary(conn: sqlite3.Connection, tables: Set[str]) -> List[Dict[str, Any]]:
        counts: Dict[str, int] = {}
        if "knowledge" in tables:
            for r in conn.execute(
                "SELECT layer, COUNT(*) AS cnt FROM knowledge WHERE superseded_by IS NULL GROUP BY layer"
            ).fetchall():
                counts[str(r["layer"])] = int(r["cnt"])
        out: List[Dict[str, Any]] = []
        for layer, auth in LAYER_AUTHORITY.items():
            out.append(
                {
                    "label": f"{layer} (auth={auth})",
                    "scope": "immutable" if layer in ("P0", "P1") else "governed",
                    "reserved": layer in ("P0", "P1"),
                    "len": counts.get(layer, 0),
                    "size_limit": auth,
                }
            )
        return out

    def _populate_curated(
        self,
        conn: sqlite3.Connection,
        tables: Set[str],
        limit: int,
        include_extra: bool,
        nodes_by_id: Dict[str, Dict[str, Any]],
        add_edge,
    ) -> None:
        if "rules" in tables:
            for r in conn.execute(
                "SELECT id, priority, layer, scope, text, source, created_at "
                "FROM rules WHERE enabled = 1 ORDER BY priority ASC, id ASC LIMIT ?",
                (max(20, limit // 5),),
            ).fetchall():
                rnode = self._rule_to_node(r)
                nodes_by_id[rnode["id"]] = rnode

        entity_ids: List[int] = []
        if "entities" in tables:
            for r in conn.execute(
                "SELECT id, label, type, type_parent, properties, knowledge_id, created_at "
                "FROM entities ORDER BY id DESC LIMIT ?",
                (max(30, limit // 3),),
            ).fetchall():
                enode = self._entity_to_node(r)
                nodes_by_id[enode["id"]] = enode
                entity_ids.append(int(r["id"]))

        if "knowledge" in tables:
            k_budget = max(40, limit - len(nodes_by_id))
            where_k = "" if include_extra else "WHERE superseded_by IS NULL "
            krows = conn.execute(
                "SELECT id, type, content, source, session_id, created_at, layer, who, "
                "confidence, ttl_days, superseded_by "
                f"FROM knowledge {where_k}"
                "ORDER BY CASE layer WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 "
                "WHEN 'P3' THEN 3 WHEN 'P4' THEN 4 WHEN 'P5' THEN 5 ELSE 6 END, id DESC LIMIT ?",
                (k_budget,),
            ).fetchall()
            for r in krows:
                knode = self._knowledge_to_node(r)
                nodes_by_id[knode["id"]] = knode

            # Connect supersede lineage chains & shared source/session links
            by_source: Dict[str, List[str]] = {}
            by_session: Dict[str, List[str]] = {}
            for r in krows:
                kid_key = f"k:{r['id']}"
                if r["superseded_by"] is not None:
                    add_edge(kid_key, f"k:{r['superseded_by']}", "superseded_by", 1.1)
                if r["source"]:
                    by_source.setdefault(str(r["source"]), []).append(kid_key)
                if r["session_id"]:
                    by_session.setdefault(str(r["session_id"]), []).append(kid_key)

            for group_map, rel_name in ((by_source, "same_source"), (by_session, "same_session")):
                for _, members in group_map.items():
                    if len(members) < 2:
                        continue
                    anchor = members[0]
                    for m in members[1:12]:
                        add_edge(anchor, m, rel_name, 0.7)

        if "entities" in tables and "knowledge" in tables:
            for r in conn.execute(
                "SELECT id, knowledge_id FROM entities WHERE knowledge_id IS NOT NULL LIMIT ?",
                (limit,),
            ).fetchall():
                add_edge(f"ent:{r['id']}", f"k:{r['knowledge_id']}", "grounded_in", 1.15)

        if "relations" in tables and entity_ids:
            for r in conn.execute(
                "SELECT source_id, target_id, type FROM relations LIMIT 2000"
            ).fetchall():
                add_edge(f"ent:{r['source_id']}", f"ent:{r['target_id']}", r["type"], 1.0)

        if include_extra and "episodes" in tables and len(nodes_by_id) < limit:
            ep_cap = min(80, limit - len(nodes_by_id))
            for r in conn.execute(
                "SELECT id, session_id, kind, payload, created_at FROM episodes ORDER BY id DESC LIMIT ?",
                (ep_cap,),
            ).fetchall():
                ep_key = f"ep:{r['id']}"
                nodes_by_id[ep_key] = {
                    "id": ep_key,
                    "raw_id": str(r["id"]),
                    "source_table": "episodes",
                    "label": _short_label(r["payload"], f"episode:{r['id']}", 40),
                    "preview": _preview_text(r["payload"], 160),
                    "kind": f"episode:{r['kind']}",
                    "group": "episode",
                    "weight": 0.8,
                    "confidence": 0.9,
                    "created_at": r["created_at"] or "",
                    "pinned": False,
                    "meta": {"session_id": r["session_id"] or "", "kind": r["kind"]},
                }

    def _populate_ontology(
        self,
        conn: sqlite3.Connection,
        tables: Set[str],
        limit: int,
        nodes_by_id: Dict[str, Dict[str, Any]],
        add_edge,
    ) -> None:
        if "entities" in tables:
            for r in conn.execute(
                "SELECT id, label, type, type_parent, properties, knowledge_id, created_at "
                "FROM entities ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall():
                enode = self._entity_to_node(r)
                nodes_by_id[enode["id"]] = enode
        if "rules" in tables and len(nodes_by_id) < limit:
            for r in conn.execute(
                "SELECT id, priority, layer, scope, text, source, created_at "
                "FROM rules WHERE enabled = 1 ORDER BY priority ASC LIMIT ?",
                (max(10, limit - len(nodes_by_id)),),
            ).fetchall():
                rnode = self._rule_to_node(r)
                nodes_by_id[rnode["id"]] = rnode
        if "relations" in tables:
            for r in conn.execute("SELECT source_id, target_id, type FROM relations LIMIT 3000").fetchall():
                add_edge(f"ent:{r['source_id']}", f"ent:{r['target_id']}", r["type"], 1.0)

    def _populate_search(
        self,
        conn: sqlite3.Connection,
        tables: Set[str],
        query: str,
        limit: int,
        nodes_by_id: Dict[str, Dict[str, Any]],
        add_edge,
    ) -> None:
        like_pat = f"%{_escape_like(query)}%"
        matched_k: Dict[int, sqlite3.Row] = {}
        if "knowledge" in tables:
            fts_q = _fts_query_tokens(query)
            if fts_q and "knowledge_fts" in tables:
                try:
                    for r in conn.execute(
                        "SELECT k.id, k.type, k.content, k.source, k.session_id, k.created_at, "
                        "k.layer, k.who, k.confidence, k.ttl_days, k.superseded_by "
                        "FROM knowledge_fts f JOIN knowledge k ON k.id = f.rowid "
                        "WHERE knowledge_fts MATCH ? AND k.superseded_by IS NULL "
                        "ORDER BY bm25(knowledge_fts) LIMIT ?",
                        (fts_q, min(120, limit)),
                    ).fetchall():
                        matched_k[int(r["id"])] = r
                except sqlite3.Error:
                    pass
            if len(matched_k) < min(60, limit):
                for r in conn.execute(
                    "SELECT id, type, content, source, session_id, created_at, layer, who, "
                    "confidence, ttl_days, superseded_by FROM knowledge "
                    "WHERE superseded_by IS NULL AND content LIKE ? ESCAPE '\\' "
                    "ORDER BY id DESC LIMIT ?",
                    (like_pat, min(100, limit)),
                ).fetchall():
                    matched_k.setdefault(int(r["id"]), r)

        for r in matched_k.values():
            knode = self._knowledge_to_node(r)
            nodes_by_id[knode["id"]] = knode

        if "entities" in tables and len(nodes_by_id) < limit:
            ent_rows = conn.execute(
                "SELECT id, label, type, type_parent, properties, knowledge_id, created_at "
                "FROM entities WHERE label LIKE ? ESCAPE '\\' OR properties LIKE ? ESCAPE '\\' LIMIT ?",
                (like_pat, like_pat, max(10, limit - len(nodes_by_id))),
            ).fetchall()
            matched_eids: Set[int] = set()
            for r in ent_rows:
                enode = self._entity_to_node(r)
                nodes_by_id[enode["id"]] = enode
                matched_eids.add(int(r["id"]))
            if "relations" in tables and matched_eids:
                ph = ",".join("?" for _ in matched_eids)
                eids_list = list(matched_eids)
                rels = conn.execute(
                    f"SELECT source_id, target_id, type FROM relations "
                    f"WHERE source_id IN ({ph}) OR target_id IN ({ph}) LIMIT 300",
                    eids_list + eids_list,
                ).fetchall()
                missing_eids = {
                    int(x)
                    for rr in rels
                    for x in (rr["source_id"], rr["target_id"])
                    if int(x) not in matched_eids
                }
                if missing_eids:
                    mph = ",".join("?" for _ in missing_eids)
                    for er in conn.execute(
                        f"SELECT id, label, type, type_parent, properties, knowledge_id, created_at "
                        f"FROM entities WHERE id IN ({mph})",
                        list(missing_eids),
                    ).fetchall():
                        en = self._entity_to_node(er)
                        nodes_by_id[en["id"]] = en
                for rr in rels:
                    add_edge(f"ent:{rr['source_id']}", f"ent:{rr['target_id']}", rr["type"], 1.0)

        if "rules" in tables and len(nodes_by_id) < limit:
            for r in conn.execute(
                "SELECT id, priority, layer, scope, text, source, created_at "
                "FROM rules WHERE enabled = 1 AND text LIKE ? ESCAPE '\\' LIMIT 40",
                (like_pat,),
            ).fetchall():
                rn = self._rule_to_node(r)
                nodes_by_id[rn["id"]] = rn

    def get_node_detail(self, node_key: str) -> Dict[str, Any]:
        """Return full content, governance metadata, audit trail, and neighbors."""
        conn = self._open()
        try:
            tables = _table_names(conn)
            if node_key.startswith("k:"):
                kid = int(node_key[2:])
                row = conn.execute("SELECT * FROM knowledge WHERE id = ?", (kid,)).fetchone()
                if row is None:
                    raise KeyError(node_key)
                neighbors: List[Dict[str, Any]] = []
                if row["superseded_by"] is not None:
                    neighbors.append(
                        {
                            "id": f"k:{row['superseded_by']}",
                            "label": f"k:{row['superseded_by']}",
                            "rel": "superseded_by",
                            "group": str(row["layer"]),
                        }
                    )
                if "entities" in tables:
                    for er in conn.execute(
                        "SELECT id, label FROM entities WHERE knowledge_id = ? LIMIT 20", (kid,)
                    ).fetchall():
                        neighbors.append(
                            {"id": f"ent:{er['id']}", "label": er["label"], "rel": "grounded_entity", "group": "entity"}
                        )
                sources = []
                if row["source"]:
                    sources.append({"system": row["source"], "path": row["session_id"] or ""})
                return {
                    "id": node_key,
                    "raw_id": str(kid),
                    "kind": f"{row['layer']} {row['type']}",
                    "group": str(row["layer"]) if str(row["layer"]) in GROUP_COLOR_MAP else "P5",
                    "title": _short_label(row["content"], f"#{kid}", 72),
                    "content": row["content"],
                    "concepts": [],
                    "metadata": {
                        "layer": row["layer"],
                        "authority": LAYER_AUTHORITY.get(str(row["layer"]), 0),
                        "who": row["who"],
                        "type": row["type"],
                        "confidence": row["confidence"],
                        "ttl_days": row["ttl_days"],
                        "superseded_by": row["superseded_by"],
                        "superseded_reason": row["superseded_reason"],
                        "created_at": row["created_at"],
                    },
                    "sources": sources,
                    "neighbors": neighbors,
                }

            if node_key.startswith("ent:"):
                eid = int(node_key[4:])
                row = conn.execute("SELECT * FROM entities WHERE id = ?", (eid,)).fetchone()
                if row is None:
                    raise KeyError(node_key)
                neighbors = []
                if "relations" in tables:
                    rels = conn.execute(
                        "SELECT r.source_id, r.target_id, r.type, s.label AS slabel, d.label AS dlabel "
                        "FROM relations r "
                        "LEFT JOIN entities s ON s.id = r.source_id "
                        "LEFT JOIN entities d ON d.id = r.target_id "
                        "WHERE r.source_id = ? OR r.target_id = ? LIMIT 40",
                        (eid, eid),
                    ).fetchall()
                    for rr in rels:
                        if int(rr["source_id"]) == eid:
                            neighbors.append(
                                {"id": f"ent:{rr['target_id']}", "label": rr["dlabel"] or str(rr["target_id"]), "rel": rr["type"], "group": "entity"}
                            )
                        else:
                            neighbors.append(
                                {"id": f"ent:{rr['source_id']}", "label": rr["slabel"] or str(rr["source_id"]), "rel": f"<- {rr['type']}", "group": "entity"}
                            )
                if row["knowledge_id"] is not None:
                    neighbors.append(
                        {"id": f"k:{row['knowledge_id']}", "label": f"knowledge #{row['knowledge_id']}", "rel": "grounded_in", "group": "P5"}
                    )
                return {
                    "id": node_key,
                    "raw_id": str(eid),
                    "kind": f"entity:{row['type']}",
                    "group": "entity",
                    "title": row["label"],
                    "content": row["properties"] or row["label"],
                    "concepts": [],
                    "metadata": {
                        "type": row["type"],
                        "type_parent": row["type_parent"],
                        "knowledge_id": row["knowledge_id"],
                        "created_at": row["created_at"],
                    },
                    "sources": [],
                    "neighbors": neighbors,
                }

            if node_key.startswith("rule:"):
                rid = int(node_key[5:])
                row = conn.execute("SELECT * FROM rules WHERE id = ?", (rid,)).fetchone()
                if row is None:
                    raise KeyError(node_key)
                return {
                    "id": node_key,
                    "raw_id": str(rid),
                    "kind": f"rule ({row['layer']})",
                    "group": "rule",
                    "title": _short_label(row["text"], f"rule #{rid}", 72),
                    "content": row["text"],
                    "concepts": [],
                    "metadata": {
                        "layer": row["layer"],
                        "priority": row["priority"],
                        "scope": row["scope"],
                        "condition": row["condition"],
                        "source": row["source"],
                        "enabled": bool(row["enabled"]),
                        "created_at": row["created_at"],
                    },
                    "sources": [],
                    "neighbors": [],
                }

            raise KeyError(node_key)
        finally:
            conn.close()

    def poll_cursor(self) -> Dict[str, int]:
        """Collect read-only monotonic max IDs for live SSE diffing."""
        conn = self._open()
        try:
            tables = _table_names(conn)
            cur = {"k": 0, "ent": 0, "rel": 0, "rule": 0, "ep": 0, "audit": 0}
            mapping = (
                ("k", "knowledge"),
                ("ent", "entities"),
                ("rel", "relations"),
                ("rule", "rules"),
                ("ep", "episodes"),
                ("audit", "audit_log"),
            )
            for key, tbl in mapping:
                if tbl in tables:
                    cur[key] = int(conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {tbl}").fetchone()[0])
            return cur
        finally:
            conn.close()

    def diff_events(self, prev: Dict[str, int], curr: Dict[str, int]) -> List[Dict[str, Any]]:
        """Return new knowledge/audit/graph events between two cursors."""
        events: List[Dict[str, Any]] = []
        conn = self._open()
        try:
            tables = _table_names(conn)
            if curr["k"] > prev.get("k", 0) and "knowledge" in tables:
                for r in conn.execute(
                    "SELECT id, type, layer, content, created_at FROM knowledge WHERE id > ? ORDER BY id ASC LIMIT 10",
                    (prev.get("k", 0),),
                ).fetchall():
                    events.append(
                        {
                            "type": "memory_added",
                            "node_id": f"k:{r['id']}",
                            "kind": f"{r['layer']} {r['type']}",
                            "label": _short_label(r["content"], f"#{r['id']}", 54),
                            "concepts": [],
                            "created_at": r["created_at"] or "",
                        }
                    )
            if curr["audit"] > prev.get("audit", 0) and "audit_log" in tables:
                for r in conn.execute(
                    "SELECT id, action, knowledge_id, layer, who, detail, denied, created_at "
                    "FROM audit_log WHERE id > ? ORDER BY id ASC LIMIT 10",
                    (prev.get("audit", 0),),
                ).fetchall():
                    events.append(
                        {
                            "type": "audit_event",
                            "id": int(r["id"]),
                            "action": r["action"],
                            "target_id": f"k:{r['knowledge_id']}" if r["knowledge_id"] else (r["layer"] or ""),
                            "caller": r["who"] or "system",
                            "denied": bool(r["denied"]),
                            "detail": r["detail"] or "",
                            "created_at": r["created_at"] or "",
                        }
                    )
            if (
                curr["ent"] > prev.get("ent", 0)
                or curr["rel"] > prev.get("rel", 0)
                or curr["rule"] > prev.get("rule", 0)
            ):
                events.append({"type": "graph_changed"})
            return events
        finally:
            conn.close()


# Embedded zero-CDN 3D/2D interactive UI HTML so wheel builds require zero static package data.
_UI_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>p-layer Memory Graph</title>
<style>
:root {
  --canvas: #07080a;
  --surface: #0d0d0d;
  --surface-elevated: #121316;
  --surface-card: #16181c;
  --hairline: #242728;
  --hairline-soft: rgba(255, 255, 255, 0.08);
  --hairline-strong: rgba(255, 255, 255, 0.18);
  --ink: #f4f4f6;
  --body: #cdcdcd;
  --mute: #9c9c9d;
  --ash: #6a6b6c;
  --accent-blue: #57c1ff;
  --accent-blue-soft: rgba(87, 193, 255, 0.14);
  --accent-green: #59d499;
  --accent-red: #ff6161;
  --accent-red-soft: rgba(255, 97, 97, 0.16);
  --radius-sm: 6px;
  --radius-md: 8px;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; overflow: hidden; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  background: var(--canvas);
  color: var(--body);
  font-size: 13px;
  line-height: 1.5;
  display: flex;
  flex-direction: column;
}
.topbar {
  min-height: 52px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 16px;
  background: var(--surface);
  border-bottom: 1px solid var(--hairline);
  flex-wrap: wrap;
}
.brand { display: flex; align-items: center; gap: 8px; }
.brand h1 { font-size: 15px; font-weight: 600; color: var(--ink); }
.ro-tag {
  font-size: 11px; font-weight: 600; padding: 2px 7px; border-radius: 4px;
  background: var(--accent-blue-soft); color: var(--accent-blue);
}
.search-wrap { position: relative; flex: 1; min-width: 180px; max-width: 340px; }
.search-input {
  width: 100%; height: 32px; background: var(--surface-elevated);
  border: 1px solid var(--hairline); border-radius: var(--radius-md);
  color: var(--ink); font: inherit; padding: 0 10px;
}
.segmented {
  display: inline-flex; background: var(--surface-elevated);
  border: 1px solid var(--hairline); border-radius: var(--radius-md); padding: 2px; gap: 2px;
}
.seg-btn {
  background: transparent; border: none; color: var(--mute); font: inherit;
  font-size: 12px; font-weight: 500; padding: 4px 10px; border-radius: var(--radius-sm); cursor: pointer;
}
.seg-btn.active { background: var(--surface-card); color: var(--ink); }
.btn {
  height: 32px; padding: 0 10px; border-radius: var(--radius-md);
  border: 1px solid var(--hairline); background: var(--surface-elevated);
  color: var(--ink); font: inherit; font-size: 12px; font-weight: 500; cursor: pointer;
}
.spacer { flex: 1; }
.sse-pill {
  display: inline-flex; align-items: center; gap: 6px; font-size: 11px; font-weight: 500;
  padding: 4px 9px; border-radius: 999px; border: 1px solid var(--hairline);
  background: var(--surface-elevated); color: var(--mute);
}
.sse-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--ash); }
.sse-pill.live { color: var(--accent-green); border-color: rgba(89, 212, 153, 0.35); }
.sse-pill.live .sse-dot { background: var(--accent-green); }
.stage {
  flex: 1; min-height: 0; display: grid; grid-template-columns: 272px minmax(0, 1fr) 0px;
}
.stage.inspector-open { grid-template-columns: 272px minmax(0, 1fr) 340px; }
.rail, .inspector { background: var(--surface); overflow-y: auto; min-width: 0; }
.rail { border-right: 1px solid var(--hairline); }
.inspector { border-left: 1px solid var(--hairline); }
.rail-inner, .inspector-inner { padding: 14px; display: flex; flex-direction: column; gap: 14px; }
.stage:not(.inspector-open) .inspector-inner { display: none; }
.section-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; }
.section-head h2 { font-size: 12px; font-weight: 600; color: var(--ink); }
.num { font-variant-numeric: tabular-nums; font-size: 11px; color: var(--mute); }
.stats-strip { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.stat-box { background: var(--surface-elevated); border: 1px solid var(--hairline-soft); border-radius: var(--radius-md); padding: 8px 10px; }
.stat-val { font-size: 16px; font-weight: 600; color: var(--ink); font-variant-numeric: tabular-nums; }
.stat-lbl { font-size: 11px; color: var(--mute); }
.filter-list, .slot-list, .feed-list { display: flex; flex-direction: column; gap: 4px; }
.filter-chip {
  display: flex; align-items: center; justify-content: space-between; padding: 5px 8px;
  border-radius: var(--radius-sm); border: 1px solid transparent; background: transparent;
  color: var(--body); font: inherit; font-size: 12px; cursor: pointer;
}
.filter-chip.active { background: var(--surface-elevated); border-color: var(--hairline); }
.filter-chip.muted { opacity: 0.4; }
.chip-left { display: flex; align-items: center; gap: 8px; }
.swatch { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
.slot-item, .feed-item {
  background: var(--surface-elevated); border: 1px solid var(--hairline-soft);
  border-radius: var(--radius-md); padding: 6px 9px; font-size: 12px;
}
.feed-item.denied { border-color: rgba(255, 97, 97, 0.45); background: var(--accent-red-soft); }
.canvas-wrap {
  position: relative; min-width: 0; min-height: 0;
  background: radial-gradient(circle at 50% 42%, #10141c 0%, var(--canvas) 72%);
  overflow: hidden;
}
#graphCanvas { width: 100%; height: 100%; display: block; cursor: grab; }
.hover-tip {
  position: absolute; pointer-events: none; background: rgba(16, 17, 22, 0.96);
  border: 1px solid var(--hairline-strong); border-radius: var(--radius-md);
  padding: 8px 10px; max-width: 280px; z-index: 30; display: none;
}
.hover-tip.visible { display: block; }
.insp-body {
  background: var(--surface-card); border: 1px solid var(--hairline-soft);
  border-radius: var(--radius-md); padding: 10px 12px; font-size: 13px; color: var(--ink);
  white-space: pre-wrap; word-break: break-word; max-height: 240px; overflow-y: auto;
}
.kv-grid { display: grid; grid-template-columns: auto 1fr; gap: 4px 10px; font-size: 12px; }
.kv-k { color: var(--mute); }
.kv-v { color: var(--ink); word-break: break-word; }
.neighbor-chip {
  background: var(--surface-elevated); border: 1px solid var(--hairline);
  border-radius: var(--radius-sm); padding: 4px 8px; font-size: 11px; color: var(--ink);
  cursor: pointer; margin: 2px;
}
</style>
</head>
<body>
<header class="topbar">
  <div class="brand"><h1>p-layer Memory Graph</h1><span class="ro-tag">READ-ONLY</span></div>
  <div class="search-wrap"><input id="searchInput" class="search-input" type="search" placeholder="Search knowledge, entities, rules ( / )"></div>
  <div class="segmented">
    <button class="seg-btn active" data-mode="curated">Governed</button>
    <button class="seg-btn" data-mode="ontology">Ontology</button>
    <button class="seg-btn" data-mode="all">All (+History)</button>
  </div>
  <div class="segmented">
    <button class="seg-btn active" id="dim3Btn">3D Orbit</button>
    <button class="seg-btn" id="dim2Btn">2D Map</button>
  </div>
  <button id="fitBtn" class="btn">Fit</button>
  <button id="pauseBtn" class="btn">Pause</button>
  <div class="spacer"></div>
  <span id="ssePill" class="sse-pill"><span class="sse-dot"></span><span id="sseText">SSE Connecting</span></span>
</header>
<div class="stage" id="stage">
  <aside class="rail">
    <div class="rail-inner">
      <div class="stats-strip">
        <div class="stat-box"><div class="stat-val" id="statRendered">—</div><div class="stat-lbl">Rendered Nodes</div></div>
        <div class="stat-box"><div class="stat-val" id="statEdges">—</div><div class="stat-lbl">Edges</div></div>
        <div class="stat-box"><div class="stat-val" id="statActive">—</div><div class="stat-lbl">Active Entries</div></div>
        <div class="stat-box"><div class="stat-val" id="statDenied">—</div><div class="stat-lbl">Denied Writes</div></div>
      </div>
      <div><div class="section-head"><h2>Layers & Groups</h2></div><div class="filter-list" id="filterList"></div></div>
      <div><div class="section-head"><h2>Layer Authority</h2></div><div class="slot-list" id="slotList"></div></div>
      <div><div class="section-head"><h2>Live Audit Log</h2><span class="num" id="feedCount">0</span></div><div class="feed-list" id="feedList"></div></div>
    </div>
  </aside>
  <main class="canvas-wrap" id="canvasWrap">
    <canvas id="graphCanvas"></canvas>
    <div id="hoverTip" class="hover-tip"><div id="hoverTitle" style="font-weight:600;color:var(--ink)"></div><div id="hoverSub" style="font-size:11px;color:var(--mute)"></div></div>
  </main>
  <aside class="inspector"><div class="inspector-inner" id="inspectorInner"></div></aside>
</div>
<script>
"use strict";
const $ = (id) => document.getElementById(id);
const canvas = $("graphCanvas"), ctx = canvas.getContext("2d");
const state = {
  mode: "curated", query: "", dim: 3, dimMix: 1.0, paused: false,
  hiddenGroups: new Set(), rawData: null, nodes: [], edges: [],
  nodeById: new Map(), groupColors: new Map(), adjMap: new Map(),
  hoveredId: null, selectedId: null, liveFeed: [], pulseNodes: new Map(),
  width: 800, height: 600, simAlpha: 1.0,
  cam: { yaw: 0.45, pitch: -0.25, zoom: 1.0, panX: 0, panY: 0, targetX: 0, targetY: 0, targetZ: 0, goalTargetX: 0, goalTargetY: 0, goalTargetZ: 0 },
  pointer: { down: false, moved: false, dragNode: null, lastX: 0, lastY: 0, startX: 0, startY: 0 }
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function loadGraph() {
  const p = new URLSearchParams({ mode: state.mode, limit: "800" });
  if (state.query) p.set("q", state.query);
  const res = await fetch(`/api/graph?${p}`, { cache: "no-store" });
  const data = await res.json();
  state.rawData = data;
  state.groupColors = new Map(data.groups.map((g) => [g.key, g.color]));
  applyFilter();
  renderSidebar(data);
}

function applyFilter() {
  if (!state.rawData) return;
  const existing = new Map(state.nodes.map((n) => [n.id, n]));
  const vis = state.rawData.nodes.filter((n) => !state.hiddenGroups.has(n.group));
  const ids = new Set(vis.map((n) => n.id));
  const ga = Math.PI * (3 - Math.sqrt(5)), total = Math.max(1, vis.length), spread = Math.max(150, Math.sqrt(total) * 18);
  state.nodes = vis.map((n, i) => {
    const p = existing.get(n.id);
    if (p) return { ...n, x: p.x, y: p.y, z: p.z, vx: 0, vy: 0, vz: 0 };
    const yn = 1 - (i / Math.max(1, total - 1)) * 2, r = Math.sqrt(Math.max(0, 1 - yn * yn)), th = ga * i;
    return { ...n, x: Math.cos(th) * r * spread, y: yn * spread * 0.85, z: Math.sin(th) * r * spread, vx: 0, vy: 0, vz: 0 };
  });
  state.nodeById = new Map(state.nodes.map((n) => [n.id, n]));
  state.edges = state.rawData.edges.filter((e) => ids.has(e.source) && ids.has(e.target)).map((e) => ({
    ...e, sNode: state.nodeById.get(e.source), tNode: state.nodeById.get(e.target)
  }));
  state.adjMap = new Map();
  for (const e of state.edges) {
    if (!state.adjMap.has(e.source)) state.adjMap.set(e.source, new Set());
    if (!state.adjMap.has(e.target)) state.adjMap.set(e.target, new Set());
    state.adjMap.get(e.source).add(e.target);
    state.adjMap.get(e.target).add(e.source);
  }
  state.simAlpha = 0.85;
  $("statRendered").textContent = String(state.nodes.length);
  $("statEdges").textContent = String(state.edges.length);
}

function renderSidebar(data) {
  const st = data.stats || {};
  $("statActive").textContent = String(st.active_knowledge ?? 0);
  $("statDenied").textContent = String(st.denied_writes ?? 0);
  $("filterList").innerHTML = data.groups.filter((g) => g.count > 0).map((g) => {
    const h = state.hiddenGroups.has(g.key);
    return `<button class="filter-chip ${h ? "muted" : "active"}" data-group="${esc(g.key)}">
      <span class="chip-left"><span class="swatch" style="background:${esc(g.color)}"></span><span>${esc(g.label)}</span></span>
      <span class="num">${g.count}</span>
    </button>`;
  }).join("");
  $("filterList").querySelectorAll("[data-group]").forEach((b) => b.addEventListener("click", () => {
    const g = b.dataset.group;
    if (state.hiddenGroups.has(g)) state.hiddenGroups.delete(g); else state.hiddenGroups.add(g);
    renderSidebar(state.rawData);
    applyFilter();
  }));
  $("slotList").innerHTML = (data.slots || []).map((s) =>
    `<div class="slot-item"><div style="display:flex;justify-content:space-between"><b>${esc(s.label)}</b><span>${s.len} entries</span></div></div>`
  ).join("");
  if (data.audit?.length && state.liveFeed.length === 0) {
    state.liveFeed = data.audit.slice(0, 15).map((a) => ({
      title: `${a.action} ${a.target_id || ""}`.trim(),
      sub: `${a.caller} · ${a.denied ? "DENIED" : "ok"}`,
      denied: !!a.denied
    }));
  }
  $("feedCount").textContent = String(state.liveFeed.length);
  $("feedList").innerHTML = state.liveFeed.slice(0, 15).map((f) =>
    `<div class="feed-item ${f.denied ? "denied" : ""}"><div>${esc(f.title)}</div><div style="font-size:11px;color:var(--mute)">${esc(f.sub)}</div></div>`
  ).join("");
}

async function openInspector(id) {
  state.selectedId = id;
  $("stage").classList.add("inspector-open");
  const d = await (await fetch(`/api/node/${encodeURIComponent(id)}`)).json();
  const meta = Object.entries(d.metadata || {}).filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([k, v]) => `<div class="kv-k">${esc(k)}</div><div class="kv-v">${esc(String(v))}</div>`).join("");
  const nbs = (d.neighbors || []).map((nb) =>
    `<button class="neighbor-chip" data-jump="${esc(nb.id)}">${esc(nb.label)} (${esc(nb.rel)})</button>`
  ).join("");
  $("inspectorInner").innerHTML = `
    <div style="display:flex;justify-content:space-between;gap:8px">
      <div><div style="font-weight:600;color:var(--ink)">${esc(d.title)}</div><div style="font-size:11px;color:var(--ash)">${esc(d.id)} · ${esc(d.kind)}</div></div>
      <button class="btn" style="height:26px;padding:0 8px" id="closeInspBtn">×</button>
    </div>
    <div class="insp-body">${esc(d.content)}</div>
    <div><div class="section-head"><h2>Neighbors</h2></div><div>${nbs || "None"}</div></div>
    <div class="kv-grid">${meta}</div>`;
  $("closeInspBtn").addEventListener("click", () => { state.selectedId = null; $("stage").classList.remove("inspector-open"); });
  $("inspectorInner").querySelectorAll("[data-jump]").forEach((b) => b.addEventListener("click", () => openInspector(b.dataset.jump)));
}

function drawFrame(now) {
  state.dimMix += ((state.dim === 3 ? 1 : 0) - state.dimMix) * 0.16;
  if (!state.paused && state.simAlpha > 0.015 && state.nodes.length) {
    const nodes = state.nodes, is2D = state.dim === 2;
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < Math.min(nodes.length, i + 90); j++) {
        const b = nodes[j], dx = a.x - b.x, dy = a.y - b.y, dz = is2D ? 0 : (a.z - b.z);
        const d2 = dx * dx + dy * dy + dz * dz + 40, f = (780 * state.simAlpha) / d2;
        a.vx += dx * f; a.vy += dy * f; a.vz += dz * f;
        b.vx -= dx * f; b.vy -= dy * f; b.vz -= dz * f;
      }
    }
    for (const e of state.edges) {
      const a = e.sNode, b = e.tNode, dx = b.x - a.x, dy = b.y - a.y, dz = is2D ? 0 : (b.z - a.z);
      const dist = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1, f = ((dist - 80) / dist) * 0.045 * state.simAlpha;
      a.vx += dx * f; a.vy += dy * f; a.vz += dz * f; b.vx -= dx * f; b.vy -= dy * f; b.vz -= dz * f;
    }
    for (const n of nodes) {
      if (state.pointer.dragNode === n) continue;
      n.vx = (n.vx - n.x * 0.008 * state.simAlpha) * 0.82;
      n.vy = (n.vy - n.y * 0.008 * state.simAlpha) * 0.82;
      n.vz = is2D ? n.vz * 0.5 : (n.vz - n.z * 0.008 * state.simAlpha) * 0.82;
      n.x += Math.max(-16, Math.min(16, n.vx)); n.y += Math.max(-16, Math.min(16, n.vy)); n.z += Math.max(-16, Math.min(16, n.vz));
    }
    state.simAlpha *= 0.99;
  }
  const mix = state.dimMix, cy = Math.cos(state.cam.yaw * mix), sy = Math.sin(state.cam.yaw * mix);
  const cp = Math.cos(state.cam.pitch * mix), sp = Math.sin(state.cam.pitch * mix);
  const hw = state.width * 0.5 + state.cam.panX, hh = state.height * 0.5 + state.cam.panY;
  for (const n of state.nodes) {
    const px = n.x, py = n.y, pz = n.z * mix;
    const x1 = px * cy - pz * sy, z1 = px * sy + pz * cy, y2 = py * cp - z1 * sp, z2 = py * sp + z1 * cp;
    const p = mix < 0.05 ? state.cam.zoom : (900 / Math.max(120, 900 + z2 * state.cam.zoom * mix)) * state.cam.zoom;
    n._sx = hw + x1 * p; n._sy = hh + y2 * p; n._sz = z2; n._r = Math.max(3, Math.min(14, (4 + n.weight * 2) * p));
  }
  ctx.clearRect(0, 0, state.width, state.height);
  for (const e of state.edges) {
    ctx.strokeStyle = "rgba(180,196,220,0.18)"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(e.sNode._sx, e.sNode._sy); ctx.lineTo(e.tNode._sx, e.tNode._sy); ctx.stroke();
  }
  for (const n of state.nodes.slice().sort((a, b) => b._sz - a._sz)) {
    const col = state.groupColors.get(n.group) || "#57c1ff";
    ctx.beginPath(); ctx.arc(n._sx, n._sy, n._r, 0, Math.PI * 2); ctx.fillStyle = col; ctx.fill();
  }
  requestAnimationFrame(drawFrame);
}

function resizeCanvas() {
  const r = $("canvasWrap").getBoundingClientRect();
  state.width = Math.max(200, Math.floor(r.width)); state.height = Math.max(200, Math.floor(r.height));
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  canvas.width = state.width * dpr; canvas.height = state.height * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
canvas.addEventListener("mousedown", (e) => {
  const rect = canvas.getBoundingClientRect(), sx = e.clientX - rect.left, sy = e.clientY - rect.top;
  const hit = state.nodes.find((n) => Math.hypot(sx - n._sx, sy - n._sy) <= Math.max(8, n._r + 3));
  state.pointer = { down: true, moved: false, dragNode: hit || null, lastX: sx, lastY: sy, startX: sx, startY: sy };
});
window.addEventListener("mousemove", (e) => {
  const tip = $("hoverTip");
  if (!state.pointer.down) {
    if (e.target !== canvas) { tip.classList.remove("visible"); return; }
    const rect = canvas.getBoundingClientRect(), sx = e.clientX - rect.left, sy = e.clientY - rect.top;
    const hit = state.nodes.find((n) => Math.hypot(sx - n._sx, sy - n._sy) <= Math.max(8, n._r + 3));
    if (hit) {
      $("hoverTitle").textContent = hit.label;
      $("hoverSub").textContent = `${hit.kind} · deg ${hit.degree} · ${hit.preview}`;
      tip.style.left = `${Math.min(state.width - 290, sx + 14)}px`;
      tip.style.top = `${Math.min(state.height - 80, sy + 14)}px`;
      tip.classList.add("visible");
    } else {
      tip.classList.remove("visible");
    }
    return;
  }
  tip.classList.remove("visible");
  const rect = canvas.getBoundingClientRect(), sx = e.clientX - rect.left, sy = e.clientY - rect.top;
  const dx = sx - state.pointer.lastX, dy = sy - state.pointer.lastY;
  if (Math.hypot(sx - state.pointer.startX, sy - state.pointer.startY) > 3) state.pointer.moved = true;
  if (state.pointer.dragNode) { state.pointer.dragNode.x += dx; state.pointer.dragNode.y += dy; }
  else if (state.dim === 3) { state.cam.yaw += dx * 0.007; state.cam.pitch = Math.max(-1.35, Math.min(1.35, state.cam.pitch + dy * 0.007)); }
  else { state.cam.panX += dx; state.cam.panY += dy; }
  state.pointer.lastX = sx; state.pointer.lastY = sy;
});
window.addEventListener("keydown", (e) => {
  if (document.activeElement === $("searchInput")) { if (e.key === "Escape") $("searchInput").blur(); return; }
  if (e.key === "/") { e.preventDefault(); $("searchInput").focus(); }
  else if (e.key === "2") $("dim2Btn").click();
  else if (e.key === "3") $("dim3Btn").click();
  else if (e.key === "Escape") { state.selectedId = null; $("stage").classList.remove("inspector-open"); }
});
window.addEventListener("mouseup", () => {
  if (state.pointer.down && !state.pointer.moved && state.pointer.dragNode) openInspector(state.pointer.dragNode.id);
  state.pointer.down = false; state.pointer.dragNode = null;
});
canvas.addEventListener("wheel", (e) => { e.preventDefault(); state.cam.zoom = Math.max(0.25, Math.min(4.5, state.cam.zoom * Math.exp(-e.deltaY * 0.0012))); }, { passive: false });
document.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", () => {
  state.mode = b.dataset.mode;
  document.querySelectorAll("[data-mode]").forEach((x) => x.classList.toggle("active", x === b));
  loadGraph();
}));
$("dim3Btn").addEventListener("click", () => { state.dim = 3; $("dim3Btn").classList.add("active"); $("dim2Btn").classList.remove("active"); });
$("dim2Btn").addEventListener("click", () => { state.dim = 2; $("dim2Btn").classList.add("active"); $("dim3Btn").classList.remove("active"); });
$("fitBtn").addEventListener("click", () => { state.cam.panX = 0; state.cam.panY = 0; state.cam.zoom = 1.0; });
$("pauseBtn").addEventListener("click", () => { state.paused = !state.paused; $("pauseBtn").textContent = state.paused ? "Resume" : "Pause"; });
$("searchInput").addEventListener("input", () => { state.query = $("searchInput").value.trim(); loadGraph(); });

const es = new EventSource("/api/events");
es.addEventListener("connected", () => { $("ssePill").className = "sse-pill live"; $("sseText").textContent = "SSE Live"; });
es.addEventListener("memory_added", () => loadGraph());
es.addEventListener("audit_event", (ev) => {
  const d = JSON.parse(ev.data);
  state.liveFeed.unshift({ title: `${d.denied ? "DENIED" : "AUDIT"}: ${d.action} ${d.target_id || ""}`, sub: d.caller, denied: !!d.denied });
  loadGraph();
});
window.addEventListener("resize", resizeCanvas);
resizeCanvas(); loadGraph(); requestAnimationFrame(drawFrame);
</script>
</body>
</html>
"""


def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass


def _write_sse(handler: BaseHTTPRequestHandler, event_name: str, data: Any) -> bool:
    payload = f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")
    try:
        handler.wfile.write(payload)
        handler.wfile.flush()
        return True
    except (BrokenPipeError, ConnectionResetError, OSError):
        return False


def make_handler(db_path: Path | str):
    """Build a ThreadingHTTPServer handler bound read-only to ``db_path``."""
    adapter = PLayerGraphAdapter(db_path)
    html_bytes = _UI_HTML_TEMPLATE.encode("utf-8")

    class PLayerUiHandler(BaseHTTPRequestHandler):
        server_version = "PLayerUI/1.0"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html_bytes)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                try:
                    self.wfile.write(html_bytes)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return

            if path == "/healthz":
                _send_json(self, 200, {"ok": True, "service": "p-layer-ui", "readonly": True})
                return

            if path == "/api/graph":
                mode = (qs.get("mode") or ["curated"])[0]
                query = (qs.get("q") or [""])[0]
                raw_kinds = (qs.get("kinds") or [""])[0]
                kinds = [k for k in raw_kinds.split(",") if k.strip()] if raw_kinds else None
                raw_limit = (qs.get("limit") or [str(DEFAULT_LIMIT)])[0]
                try:
                    limit = int(raw_limit)
                except ValueError:
                    _send_json(self, 400, {"error": {"status": 400, "message": "invalid limit"}})
                    return
                if limit < 1:
                    _send_json(self, 400, {"error": {"status": 400, "message": "limit must be >= 1"}})
                    return
                try:
                    payload = adapter.build_graph(mode=mode, query=query, kinds=kinds, limit=limit)
                except FileNotFoundError as exc:
                    _send_json(self, 503, {"error": {"status": 503, "message": str(exc)}})
                    return
                except sqlite3.Error as exc:
                    _send_json(self, 500, {"error": {"status": 500, "message": str(exc)}})
                    return
                _send_json(self, 200, payload)
                return

            if path.startswith("/api/node/"):
                node_key = unquote(path[len("/api/node/"):])
                if not node_key:
                    _send_json(self, 400, {"error": {"status": 400, "message": "node id required"}})
                    return
                try:
                    detail = adapter.get_node_detail(node_key)
                except KeyError:
                    _send_json(self, 404, {"error": {"status": 404, "message": "node not found"}})
                    return
                except FileNotFoundError as exc:
                    _send_json(self, 503, {"error": {"status": 503, "message": str(exc)}})
                    return
                _send_json(self, 200, detail)
                return

            if path == "/api/events":
                once = (qs.get("once") or ["0"])[0] in ("1", "true", "yes")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    cursor = adapter.poll_cursor()
                except (FileNotFoundError, sqlite3.Error) as exc:
                    _write_sse(self, "error", {"message": str(exc)})
                    return
                if not _write_sse(self, "connected", {"ok": True, "cursor": cursor}):
                    return
                if once:
                    return
                last_hb = time.monotonic()
                while True:
                    time.sleep(SSE_POLL_SECONDS)
                    try:
                        nxt = adapter.poll_cursor()
                        if nxt != cursor:
                            for ev in adapter.diff_events(cursor, nxt):
                                if not _write_sse(self, ev["type"], ev):
                                    return
                            cursor = nxt
                        now = time.monotonic()
                        if now - last_hb >= 15.0:
                            last_hb = now
                            if not _write_sse(self, "heartbeat", {"ts": _dt.datetime.now(_dt.timezone.utc).isoformat()}):
                                return
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        return
                    except (FileNotFoundError, sqlite3.Error) as exc:
                        _write_sse(self, "error", {"message": str(exc)})
                        return

            _send_json(self, 404, {"error": {"status": 404, "message": f"unknown path: {path}"}})

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return PLayerUiHandler


class PLayerUIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve_ui(
    db_path: Path | str,
    *,
    host: str = DEFAULT_UI_HOST,
    port: int = DEFAULT_UI_PORT,
    open_browser: bool = True,
    allow_remote: bool = False,
) -> int:
    """Start the read-only 2D/3D p-layer Memory Graph web UI server."""
    if not allow_remote and not is_loopback_host(host):
        raise ValueError(
            f"refusing to bind p-layer ui to non-loopback host '{host}' without --allow-remote"
        )
    server = PLayerUIServer((host, port), make_handler(db_path))
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}/"
    print(f"p-layer ui (read-only) listening on {url}  (db: {db_path})", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
