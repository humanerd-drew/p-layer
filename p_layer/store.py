"""p_layer store — SQLite-backed agent memory.

Single implementation, single schema (unlike drewgent's dual TS/Python paths
and p-layer's Pg/SQLite parity holes). WAL + foreign keys on. FTS5 is a
standalone table (no external-content triggers — the trigger-sync fragility
of drewgent/p-layer is avoided).

v2 adds p-layer's governance concepts, enforced in code rather than prose:
  layer ACLs (P0 system-only ... P6 agent+manual), supersede-not-delete,
  confidence/TTL/freshness ranking, snapshots/rollback, serendipity recall.
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import sqlite3
import struct
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .embed import Embedder, EmbeddingError, load_embedder
from .migrations import migrate

DEFAULT_DB = os.environ.get("P_LAYER_DB") or str(Path.home() / ".p_layer" / "memory.db")

KNOWLEDGE_TYPES = ("fact", "decision", "preference", "pattern", "insight")

# Layer governance: lower layer number = higher authority.
LAYER_AUTHORITY = {"P0": 100, "P1": 80, "P2": 60, "P3": 50, "P4": 40, "P5": 30, "P6": 20}

# Who may write to each layer. `who` is a principal like "system", "agent",
# "tool", "cron", "gateway", "manual", optionally "principal:detail".
LAYER_WRITERS = {
    "P0": frozenset({"system"}),
    "P1": frozenset({"system"}),
    "P2": frozenset({"system", "gateway", "cron"}),
    "P3": frozenset({"system", "gateway", "cron"}),
    "P4": frozenset({"system", "cron", "agent", "manual"}),
    "P5": frozenset({"system", "cron", "agent", "manual", "tool"}),
    "P6": frozenset({"system", "cron", "agent", "manual", "tool"}),
}

# Typed ontology edges with (source_types, target_types); None = any type.
RELATION_CONSTRAINTS: dict[str, tuple[tuple[str, ...] | None, tuple[str, ...] | None] | None] = {
    "depends_on": (None, ("tool", "script", "skill")),
    "fixed_by": (("incident",), ("pattern", "decision")),
    "caused": (("decision", "pattern"), ("incident",)),
    "led_to": (("decision", "pattern", "preference"), ("decision", "pattern", "preference")),
    "implements": (("script",), ("pattern", "decision")),
    "contradicts": (("decision", "pattern", "preference"), ("decision", "pattern", "preference")),
    "cites": (("paper",), ("paper",)),
    "references": None,
    "relates_to": None,
    "subtype_of": None,
    "belongs_to": None,
}


class WriteDenied(ValueError):
    pass


def _check_layer_write(layer: str, who: str) -> None:
    if layer not in LAYER_AUTHORITY:
        raise ValueError(f"invalid layer: {layer}; must be P0-P6")
    allowed = LAYER_WRITERS.get(layer, frozenset())
    principal = who.split(":")[0] if ":" in who else who
    if principal not in allowed and who not in allowed:
        raise WriteDenied(
            f"layer {layer} does not allow writes from '{who}'; allowed: {sorted(allowed)}"
        )


def utcnow() -> str:
    # Microsecond precision so snapshot cut-offs and ordering never collide
    # within the same second (p-layer's SQLite path has this exact bug class).
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


_KNOWLEDGE_COLS = "k.id, k.type, k.content, k.source, k.session_id, k.created_at, k.layer, k.who, k.confidence, k.ttl_days, k.superseded_by"


def _age_days(created_at: str | None) -> float:
    if not created_at:
        return 0.0
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)


def _vec_to_blob(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _blob_to_vec(blob: bytes, dims: int) -> list[float]:
    return list(struct.unpack(f"<{dims}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


def _freshness(row: dict) -> float:
    ttl = row.get("ttl_days")
    if not ttl:
        return 1.0
    return 1.0 + 0.3 * max(0.0, 1.0 - _age_days(row["created_at"]) / ttl)


# ── Additive rerank configuration ──────────────────────────────────────────
# Separates ranking signals into bounded additive boosts on top of raw RRF,
# rather than the multiplicative approach where one zero-signal kills the rest.

class RerankConfig:
    """Tunable constants for the additive reranker.

    All boost values are intentionally small relative to RRF scores (~0.016)
    so they nudge ordering without overriding retrieval relevance.

    Attributes:
        confidence_gain: Max absolute boost from confidence signal.
        confidence_center: Confidence value that produces zero boost (below = penalty, above = reward).
        recency_gain: Max absolute boost from recency signal.
        recency_window_days: Time window within which recency decays linearly from max to 0.
        enabled: When False, falls back to the legacy multiplicative scoring.
    """

    __slots__ = (
        "confidence_gain", "confidence_center",
        "recency_gain", "recency_window_days",
        "enabled",
    )

    def __init__(
        self,
        *,
        confidence_gain: float = 0.0005,
        confidence_center: float = 0.5,
        recency_gain: float = 0.0002,
        recency_window_days: float = 30.0,
        enabled: bool = True,
    ):
        self.confidence_gain = confidence_gain
        self.confidence_center = confidence_center
        self.recency_gain = recency_gain
        self.recency_window_days = recency_window_days
        self.enabled = enabled


# Module-level default — importers can override or pass per-call.
DEFAULT_RERANK = RerankConfig()


def _rerank_additive(candidates: list[dict], config: RerankConfig) -> list[dict]:
    """Pure additive rerank: raw RRF + bounded confidence/recency boosts.

    Each candidate dict must have 'rrf' (float) and 'row' (dict with
    'confidence' and 'created_at'). Returns candidates sorted by
    rerank_score desc, with rerank metadata attached.

    TTL freshness: entries with ttl_days that are still within their TTL
    window get a positive boost; entries beyond TTL get no boost (but no
    penalty beyond that — they still compete on RRF + other signals).
    """
    for c in candidates:
        row = c["row"]
        # Confidence boost: linear around center, clamped to ±gain
        conf = float(row.get("confidence") or 0.0)
        conf_boost = (conf - config.confidence_center) * config.confidence_gain
        max_pos = config.confidence_gain * (1.0 - config.confidence_center)
        max_neg = -config.confidence_gain * config.confidence_center
        conf_boost = max(max_neg, min(max_pos, conf_boost))

        # Recency boost: linear decay within window, 0 outside
        recency_boost = 0.0
        created = row.get("created_at")
        if created:
            age = _age_days(created)
            if config.recency_window_days > 0:
                recency_boost = max(0.0, 1.0 - age / config.recency_window_days) * config.recency_gain

        # TTL freshness boost: entries with ttl_days that are still fresh
        # get an additional boost, decaying as they approach expiry.
        ttl_boost = 0.0
        ttl = row.get("ttl_days")
        if ttl and created:
            age = _age_days(created)
            ttl_boost = max(0.0, 1.0 - age / float(ttl)) * config.recency_gain

        c["rerank_components"] = {
            "rrf": round(c["rrf"], 4),
            "confidence_boost": round(conf_boost, 6),
            "recency_boost": round(recency_boost, 6),
            "ttl_boost": round(ttl_boost, 6),
        }
        c["rerank_score"] = round(c["rrf"] + conf_boost + recency_boost + ttl_boost, 6)

    return sorted(
        candidates,
        key=lambda x: (-x["rerank_score"], -x["rrf"], x["row"]["id"]),
    )


def rrf_fuse(fts: list[dict], sem: list[tuple[float, int]], limit: int,
             row_lookup, *, rerank: RerankConfig | None = None) -> list[dict]:
    """Backend-agnostic RRF fusion: ranks FTS rows and semantic scores,
    applies additive rerank (confidence + recency boosts), diversifies
    3-per-type. `row_lookup` maps knowledge ids to row dicts (superseded
    rows are excluded by it).

    Args:
        rerank: Rerank configuration. Pass RerankConfig(enabled=False) to
                use the legacy multiplicative scoring. Defaults to
                DEFAULT_RERANK (additive, enabled).
    """
    if rerank is None:
        rerank = DEFAULT_RERANK
    k = 60
    scores: dict[int, dict] = {}
    for rank, row in enumerate(fts, start=1):
        entry = scores.setdefault(row["id"], {"row": row, "rrf": 0.0, "sem": None})
        entry["rrf"] += 1.0 / (k + rank)
    for rank, (sim, kid) in enumerate(sem, start=1):
        entry = scores.setdefault(kid, {"row": None, "rrf": 0.0, "sem": sim})
        entry["rrf"] += 1.0 / (k + rank)
        if entry["row"] is None:
            entry["row"] = row_lookup([kid]).get(kid)
    candidates = [e for e in scores.values() if e["row"] is not None]
    if not candidates:
        return []

    # ── Ranking ──
    if rerank.enabled:
        ordered = _rerank_additive(candidates, rerank)
    else:
        # Legacy multiplicative path (backward compat)
        ordered = sorted(
            candidates,
            key=lambda e: e["rrf"] * (0.5 + 0.5 * e["row"]["confidence"]) * _freshness(e["row"]),
            reverse=True,
        )

    # ── Diversification ──
    seen: dict[str, int] = {}
    out: list[dict] = []
    # Diversification caps per type are meaningless on a homogeneous corpus
    # (e.g. imported drewgent memory where every entry maps to 'fact') and
    # would starve recall below the requested limit. Cap only when 2+ types
    # actually compete.
    distinct_types = len({e["row"]["type"] for e in candidates})
    cap = limit if distinct_types == 1 else 3
    for entry in ordered:
        row = entry["row"]
        t = row["type"]
        if seen.get(t, 0) >= cap:
            continue
        seen[t] = seen.get(t, 0) + 1
        if rerank.enabled:
            final_score = entry["rerank_score"]
        else:
            final_score = round(
                entry["rrf"] * (0.5 + 0.5 * row["confidence"]) * _freshness(row), 4
            )
        result = {
            "id": row["id"],
            "type": row["type"],
            "content": row["content"],
            "source": row["source"],
            "layer": row["layer"],
            "who": row["who"],
            "confidence": row["confidence"],
            "created_at": row["created_at"],
            "score": final_score,
            "rrf": round(entry["rrf"], 4),
            "semantic_score": entry["sem"],
        }
        if rerank.enabled:
            result["rerank_components"] = entry["rerank_components"]
        out.append(result)
        if len(out) >= limit:
            break
    return out


def _norm_tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9가-힣]+", text.lower())
    stop = {"the", "a", "an", "of", "to", "and", "or", "for", "with", "on",
            "in", "is", "are", "was", "were", "be", "it", "this", "that"}
    return {w for w in words if w not in stop and len(w) > 1}


def _token_overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def scan_contradictions(rules: list[dict], knowledge_rows: list[dict]) -> list[dict]:
    """Backend-agnostic governance-contradiction scan (no LLM)."""
    out: list[dict] = []
    for i in range(len(rules)):
        for j in range(i + 1, len(rules)):
            r1, r2 = rules[i], rules[j]
            if r1["priority"] == r2["priority"]:
                continue
            if _token_overlap(_norm_tokens(r1["text"]), _norm_tokens(r2["text"])) >= 0.8:
                out.append({
                    "kind": "conflicting_rules",
                    "severity": "high",
                    "a": {"id": r1["id"], "layer": r1["layer"], "priority": r1["priority"], "text": r1["text"]},
                    "b": {"id": r2["id"], "layer": r2["layer"], "priority": r2["priority"], "text": r2["text"]},
                })
    for i in range(len(knowledge_rows)):
        for j in range(i + 1, len(knowledge_rows)):
            a, b = knowledge_rows[i], knowledge_rows[j]
            if a["layer"] == b["layer"]:
                continue
            if _token_overlap(_norm_tokens(a["content"]), _norm_tokens(b["content"])) >= 0.8:
                out.append({
                    "kind": "cross_layer_duplicate",
                    "severity": "medium",
                    "a": {"id": a["id"], "layer": a["layer"], "content": a["content"]},
                    "b": {"id": b["id"], "layer": b["layer"], "content": b["content"]},
                })
    return out


def compose_assemble(rules: list[dict], recent: list[dict], budget_chars: int = 12000) -> str:
    """Backend-agnostic budget-bounded context assembly: rules (priority
    order) first, then recent knowledge. Never emits partial items."""
    parts: list[str] = []
    used = 0
    for r in rules:
        text = f"[{r['layer']}] {r['text']}"
        if used + len(text) > budget_chars:
            break
        parts.append(text)
        used += len(text)
    for k in recent:
        text = f"[{k['layer']}][{k['type']}] {k['content']}"
        if used + len(text) > budget_chars:
            break
        parts.append(text)
        used += len(text)
    return "\n\n".join(parts)




class Store:
    def __init__(self, path: str | os.PathLike | None = None, embedder: Embedder | None = None):
        self.path = Path(path or DEFAULT_DB)
        self.embedder = embedder
        self._db: sqlite3.Connection | None = None
        self.last_embed_warning: str | None = None

    # ── connection ────────────────────────────────────────────
    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(str(self.path))
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            self._db = db
            migrate(db)
        return self._db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def _get_embedder(self) -> Embedder | None:
        if self.embedder is None:
            try:
                self.embedder = load_embedder()
            except ValueError:
                return None
        return self.embedder

    def _audit(self, action: str, knowledge_id: int | None = None, layer: str | None = None,
               who: str | None = None, detail: str | None = None, denied: bool = False) -> None:
        """Append to the governance audit log. Every write and every denied
        write is recorded — this is the compliance evidence for the
        'governance, not just retrieval' thesis."""
        self.db.execute(
            "INSERT INTO audit_log (action, knowledge_id, layer, who, detail, denied, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (action, knowledge_id, layer, who, detail, 1 if denied else 0, utcnow()),
        )

    # ── write path ────────────────────────────────────────────
    def add_knowledge(
        self,
        content: str,
        type: str = "fact",
        source: str | None = None,
        session_id: str | None = None,
        created_at: str | None = None,
        embed: bool = True,
        layer: str = "P5",
        who: str = "system",
        confidence: float = 1.0,
        ttl_days: int | None = None,
    ) -> int:
        """Store a semantic-memory entry under a governance layer."""
        if not content or not content.strip():
            raise ValueError("content must not be empty")
        if type not in KNOWLEDGE_TYPES:
            raise ValueError(f"type must be one of {KNOWLEDGE_TYPES}")
        try:
            _check_layer_write(layer, who)
        except WriteDenied as exc:
            # Denied writes are first-class audit evidence (governance compliance).
            self._audit("write_denied", layer=layer, who=who, detail=str(exc), denied=True)
            self.db.commit()
            raise
        db = self.db
        cur = db.execute(
            "INSERT INTO knowledge (type, content, source, session_id, created_at, layer, who, confidence, ttl_days) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (type, content, source, session_id, created_at or utcnow(), layer, who,
             max(0.0, min(1.0, float(confidence))), ttl_days),
        )
        kid = int(cur.lastrowid)
        db.execute(
            "INSERT INTO knowledge_fts (rowid, content, type) VALUES (?,?,?)",
            (kid, content, type),
        )
        if embed:
            self._maybe_embed(kid, content)
        self._audit("remember", knowledge_id=kid, layer=layer, who=who, detail=f"type={type} source={source}")
        db.commit()
        return kid

    def _maybe_embed(self, kid: int, content: str) -> None:
        emb = self._get_embedder()
        if emb is None or not emb.available():
            return
        try:
            vec = emb.embed([content[:2000]])[0]
        except EmbeddingError as exc:
            self.last_embed_warning = str(exc)
            return
        if len(vec) != emb.dimensions:
            self.last_embed_warning = f"dimension mismatch: got {len(vec)}, expected {emb.dimensions}"
            return
        self.db.execute(
            "INSERT OR REPLACE INTO embeddings "
            "(knowledge_id, model, embedding_version, dimensions, vector, created_at) VALUES (?,?,?,?,?,?)",
            (kid, emb.model, emb.embedding_version, emb.dimensions, _vec_to_blob(vec), utcnow()),
        )

    def forget(self, knowledge_id: int, reason: str | None = None) -> bool:
        """Supersede an entry (never destroy). Recall stops surfacing it; history stays."""
        db = self.db
        cur = db.execute(
            "UPDATE knowledge SET superseded_by = id, superseded_reason = ? "
            "WHERE id = ? AND superseded_by IS NULL",
            (reason, knowledge_id),
        )
        db.commit()
        ok = cur.rowcount > 0
        if ok:
            self._audit("forget", knowledge_id=knowledge_id, detail=reason or "superseded")
            db.commit()
        return ok

    def update_knowledge(
        self,
        knowledge_id: int,
        content: str | None = None,
        type: str | None = None,
        confidence: float | None = None,
        layer: str | None = None,
        who: str | None = None,
    ) -> int:
        """Supersede the old entry and insert a new one; the chain is preserved."""
        db = self.db
        row = db.execute("SELECT * FROM knowledge WHERE id = ?", (knowledge_id,)).fetchone()
        if row is None:
            raise ValueError(f"no knowledge entry #{knowledge_id}")
        if row["superseded_by"] is not None:
            raise ValueError(f"entry #{knowledge_id} is already superseded")
        new_id = self.add_knowledge(
            content if content is not None else row["content"],
            type=type or row["type"],
            source=row["source"],
            session_id=row["session_id"],
            layer=layer or row["layer"],
            who=who or row["who"],
            confidence=confidence if confidence is not None else row["confidence"],
            ttl_days=row["ttl_days"],
        )
        db.execute("UPDATE knowledge SET superseded_by = ? WHERE id = ?", (new_id, knowledge_id))
        self._audit("update", knowledge_id=knowledge_id, layer=row["layer"], who=row["who"],
                    detail=f"superseded_by={new_id}")
        db.commit()
        return new_id

    def snapshot_create(self, version_id: str, label: str | None = None) -> int:
        """Freeze the set of active entry ids under a version label."""
        db = self.db
        ids = [
            r["id"]
            for r in db.execute("SELECT id FROM knowledge WHERE superseded_by IS NULL ORDER BY id").fetchall()
        ]
        cur = db.execute(
            "INSERT INTO snapshots (version_id, label, entry_ids, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(version_id) DO UPDATE SET label = excluded.label, "
            "entry_ids = excluded.entry_ids, created_at = excluded.created_at",
            (version_id, label, json.dumps(ids), utcnow()),
        )
        db.commit()
        self._audit("snapshot_create", detail=f"version_id={version_id} entries={len(ids)}")
        db.commit()
        return int(cur.lastrowid)

    def snapshot_rollback(self, version_id: str) -> int:
        """Supersede every active entry created after the snapshot."""
        db = self.db
        snap = db.execute("SELECT * FROM snapshots WHERE version_id = ?", (version_id,)).fetchone()
        if snap is None:
            raise ValueError(f"no snapshot '{version_id}'")
        cur = db.execute(
            "UPDATE knowledge SET superseded_by = id, superseded_reason = ? "
            "WHERE superseded_by IS NULL AND created_at > ?",
            (f"rollback:{version_id}", snap["created_at"]),
        )
        db.commit()
        self._audit("snapshot_rollback", detail=f"version_id={version_id} superseded={cur.rowcount}")
        db.commit()
        return cur.rowcount

    def add_rule(
        self,
        text: str,
        priority: int = 100,
        layer: str = "P0",
        scope: str | None = None,
        condition: str | None = None,
        source: str | None = None,
    ) -> int:
        """Add a canonical rule. Lower priority = higher precedence (P0 first)."""
        db = self.db
        cur = db.execute(
            "INSERT INTO rules (priority, layer, scope, condition, text, source, created_at) VALUES (?,?,?,?,?,?,?)",
            (int(priority), layer, scope, condition, text, source, utcnow()),
        )
        db.commit()
        self._audit("rule_add", layer=layer, detail=text[:200])
        db.commit()
        return int(cur.lastrowid)

    def add_entity(self, label: str, type: str, properties: dict | None = None, knowledge_id: int | None = None) -> int:
        db = self.db
        cur = db.execute(
            "INSERT INTO entities (label, type, properties, knowledge_id, created_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(label) DO UPDATE SET type = excluded.type",
            (label, type, json.dumps(properties or {}, ensure_ascii=False), knowledge_id, utcnow()),
        )
        db.commit()
        return int(cur.lastrowid)

    def add_relation(self, source_id: int, target_id: int, rtype: str, properties: dict | None = None) -> int:
        """Add a typed edge, validated against RELATION_CONSTRAINTS."""
        if rtype not in RELATION_CONSTRAINTS:
            raise ValueError(f"unknown relation type: {rtype}")
        db = self.db
        src = db.execute("SELECT id, type, label FROM entities WHERE id = ?", (source_id,)).fetchone()
        tgt = db.execute("SELECT id, type, label FROM entities WHERE id = ?", (target_id,)).fetchone()
        if src is None or tgt is None:
            raise ValueError("relation endpoints must exist")
        constraint = RELATION_CONSTRAINTS[rtype]
        if constraint is not None:
            allowed_src, allowed_tgt = constraint
            if allowed_src is not None and src["type"] not in allowed_src:
                raise ValueError(f"{src['label']}({src['type']}) not allowed as source of {rtype}")
            if allowed_tgt is not None and tgt["type"] not in allowed_tgt:
                raise ValueError(f"{tgt['label']}({tgt['type']}) not allowed as target of {rtype}")
        cur = db.execute(
            "INSERT OR IGNORE INTO relations (source_id, target_id, type, properties, created_at) VALUES (?,?,?,?,?)",
            (source_id, target_id, rtype, json.dumps(properties or {}, ensure_ascii=False), utcnow()),
        )
        db.commit()
        return int(cur.lastrowid)

    def record_episode(self, kind: str, payload, session_id: str | None = None) -> int:
        """Append to episodic memory (sessions, incidents, retros). Immutable by convention."""
        if kind not in ("session", "incident", "retro", "event"):
            raise ValueError("kind must be one of session/incident/retro/event")
        text = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
        db = self.db
        cur = db.execute(
            "INSERT INTO episodes (session_id, kind, payload, created_at) VALUES (?,?,?,?)",
            (session_id, kind, text, utcnow()),
        )
        db.commit()
        return int(cur.lastrowid)

    # ── read path ─────────────────────────────────────────────
    def fts_search(self, query: str, limit: int = 30) -> list[dict]:
        # Sanitize FTS5 query-syntax characters out of each term: a bare
        # "?" (e.g. a trailing question mark in a real agent query) or a
        # FTS5 keyword (OR/AND/NOT/NEAR) makes the whole MATCH expression
        # raise, which used to silently degrade recall to a single-phrase
        # LIKE that almost never matched. Terms are quoted so keyword
        # collisions stay valid queries.
        terms = [t for t in (re.sub(r"[^\w]+", "", w) for w in query.split()) if len(t) > 1]
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms)
        db = self.db
        try:
            rows = db.execute(
                f"SELECT {_KNOWLEDGE_COLS} FROM knowledge_fts f JOIN knowledge k ON k.id = f.rowid "
                "WHERE knowledge_fts MATCH ? AND k.superseded_by IS NULL ORDER BY rank LIMIT ?",
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            # CJK path: unicode61 does not split CJK into words, so MATCH can
            # never hit there — fall back to per-term LIKE OR (multi-term
            # recall instead of the old never-matching single phrase).
            clauses = " OR ".join("k.content LIKE ?" for _ in terms)
            rows = db.execute(
                f"SELECT {_KNOWLEDGE_COLS} FROM knowledge k "
                f"WHERE ({clauses}) AND k.superseded_by IS NULL LIMIT ?",
                [f"%{t}%" for t in terms] + [limit],
            ).fetchall()
        return [dict(r) for r in rows]

    def semantic_search(self, query: str, limit: int = 30) -> list[tuple[float, int]]:
        emb = self._get_embedder()
        if emb is None or not emb.available():
            return []
        try:
            qvec = emb.embed([query])[0]
        except EmbeddingError:
            return []
        db = self.db
        rows = db.execute(
            "SELECT knowledge_id, vector, dimensions FROM embeddings WHERE embedding_version = ?",
            (emb.embedding_version,),
        ).fetchall()
        scored = []
        for r in rows:
            if r["dimensions"] != emb.dimensions:
                continue  # skip stale vectors from a different embedder
            sim = _cosine(qvec, _blob_to_vec(r["vector"], r["dimensions"]))
            if sim > 0.15:
                scored.append((sim, r["knowledge_id"]))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:limit]

    def _rows_by_ids(self, ids: list[int]) -> dict[int, dict]:
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        rows = self.db.execute(
            f"SELECT {_KNOWLEDGE_COLS} FROM knowledge k WHERE k.id IN ({marks}) AND k.superseded_by IS NULL",
            ids,
        ).fetchall()
        return {r["id"]: dict(r) for r in rows}

    def recall(
        self,
        query: str,
        limit: int = 10,
        use_semantic: bool | None = None,
        serendipity: bool = False,
    ) -> list[dict]:
        """Hybrid recall: FTS5 + semantic (when available), RRF-fused, ranked by
        confidence and freshness, type-diversified. Superseded entries excluded."""
        emb = self._get_embedder()
        if use_semantic is None:
            use_semantic = emb is not None and emb.available() and emb.semantic
        fts = self.fts_search(query, limit * 3)
        sem = self.semantic_search(query, limit * 3) if use_semantic else []
        out = self._rrf_fuse(fts, sem, limit)
        if serendipity and out and random.random() < 0.05:
            extra = self._serendipity_pick([r["id"] for r in out])
            if extra:
                out.append(extra)
        return out

    @staticmethod
    def _freshness(row: dict) -> float:
        return _freshness(row)

    def _serendipity_pick(self, exclude: list[int]) -> dict | None:
        marks = ",".join("?" for _ in exclude)
        row = self.db.execute(
            f"SELECT {_KNOWLEDGE_COLS} FROM knowledge k "
            f"WHERE k.superseded_by IS NULL AND k.id NOT IN ({marks}) ORDER BY RANDOM() LIMIT 1",
            exclude,
        ).fetchone()
        if row is None:
            return None
        return dict(row) | {"score": None, "semantic_score": None, "_serendipity": True}

    def _rrf_fuse(self, fts: list[dict], sem: list[tuple[float, int]], limit: int) -> list[dict]:
        return rrf_fuse(fts, sem, limit, self._rows_by_ids)

    def enabled_rules(self, limit: int = 100) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, priority, layer, scope, condition, text, source FROM rules "
            "WHERE enabled = 1 ORDER BY priority ASC, id ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_knowledge(self, limit: int = 20) -> list[dict]:
        rows = self.db.execute(
            f"SELECT {_KNOWLEDGE_COLS} FROM knowledge k "
            "WHERE k.superseded_by IS NULL ORDER BY k.created_at DESC, k.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def assemble(self, budget_chars: int = 12000, include: tuple[str, ...] = ("rules", "recent")) -> str:
        """Deterministic, budget-bounded context assembly (see compose_assemble)."""
        rules = self.enabled_rules() if "rules" in include else []
        recent = self.recent_knowledge(limit=50) if "recent" in include else []
        return compose_assemble(rules, recent, budget_chars)

    def stats(self) -> dict:
        db = self.db

        def count(table: str) -> int:
            return int(db.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])

        by_type = {}
        by_layer = {}
        for r in db.execute("SELECT type, COUNT(*) AS c FROM knowledge GROUP BY type").fetchall():
            by_type[r["type"]] = r["c"]
        for r in db.execute("SELECT layer, COUNT(*) AS c FROM knowledge GROUP BY layer").fetchall():
            by_layer[r["layer"]] = r["c"]
        emb = self._get_embedder()
        return {
            "knowledge": count("knowledge"),
            "active": count("knowledge") - int(
                db.execute("SELECT COUNT(*) AS c FROM knowledge WHERE superseded_by IS NOT NULL").fetchone()["c"]
            ),
            "by_type": by_type,
            "by_layer": by_layer,
            "embeddings": count("embeddings"),
            "embeddings_by_version": {
                r["embedding_version"]: r["c"]
                for r in db.execute(
                    "SELECT embedding_version, COUNT(*) AS c FROM embeddings GROUP BY embedding_version"
                ).fetchall()
            },
            "episodes": count("episodes"),
            "entities": count("entities"),
            "relations": count("relations"),
            "rules": count("rules"),
            "snapshots": count("snapshots"),
            "audit": count("audit_log"),
            "schema_version": max(migrate(db)),
            "embedder": emb.name if emb is not None else None,
            "embed_warning": self.last_embed_warning,
        }

    # ── governance read path ──────────────────────────────────
    def audit_log(self, limit: int = 50, denied_only: bool = False) -> list[dict]:
        """Recent audit entries — every write, every denied write."""
        sql = "SELECT id, action, knowledge_id, layer, who, detail, denied, created_at FROM audit_log"
        if denied_only:
            sql += " WHERE denied = 1"
        sql += " ORDER BY id DESC LIMIT ?"
        rows = self.db.execute(sql, (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def contradictions(self) -> list[dict]:
        """Heuristic governance-contradiction scan (no LLM):

        1. enabled rules with high token overlap but different priority —
           a precedence conflict that silently changes behavior;
        2. active knowledge near-duplicates living in different layers —
           a layer-boundary smell (same fact, two authorities).
        """
        return scan_contradictions(self.enabled_rules(limit=500),
                                   [dict(r) for r in self.db.execute(
                                       "SELECT id, layer, type, content FROM knowledge k WHERE k.superseded_by IS NULL"
                                   ).fetchall()])

    def compile_wiki(self, out_dir: str | Path) -> dict:
        """Compile active memory into the P5 wiki: per-layer markdown pages
        with provenance, plus rules.md and INDEX.md (the compiled-knowledge
        layer of the P0-P6 design, fully offline)."""
        out = Path(out_dir)
        wiki = out / "P5-ego" / "wiki" / "compiled"
        wiki.mkdir(parents=True, exist_ok=True)
        rows = self.db.execute(
            f"SELECT {_KNOWLEDGE_COLS} FROM knowledge k "
            "WHERE k.superseded_by IS NULL ORDER BY k.layer, k.created_at DESC"
        ).fetchall()
        by_layer: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            by_layer[r["layer"]].append(dict(r))

        files: dict[str, int] = {}
        for layer in sorted(by_layer):
            lines = [f"# {layer}", ""]
            for e in by_layer[layer]:
                lines += [
                    f"## [{e['type']}] {e['content']}",
                    "",
                    f"- who: {e['who']} · created: {e['created_at']} · confidence: {e['confidence']}",
                ]
                if e["source"]:
                    lines.append(f"- source: {e['source']}")
                lines.append("")
            rel = f"P5-ego/wiki/compiled/{layer}.md"
            (wiki / f"{layer}.md").write_text("\n".join(lines))
            files[rel] = len(by_layer[layer])

        rules = self.enabled_rules()
        if rules:
            lines = ["# Rules (priority order)", ""]
            for r in rules:
                lines += [f"## [{r['layer']}] {r['text']}", f"- priority: {r['priority']}", ""]
            (wiki / "rules.md").write_text("\n".join(lines))
            files["P5-ego/wiki/compiled/rules.md"] = len(rules)

        idx = ["# Compiled Wiki Index", "", f"Generated: {utcnow()}", ""]
        for name, n in sorted(files.items()):
            idx.append(f"- {name} ({n} entries)")
        (out / "INDEX.md").write_text("\n".join(idx) + "\n")
        files["INDEX.md"] = sum(files.values())
        return {"dir": str(out), "files": files, "entries": sum(files.values())}

    # ── graph & inference (drewgent graph_query.py parity) ────
    def _find_entities(self, query: str, limit: int = 20) -> list[dict]:
        """Fuzzy entity lookup: exact label first, then prefix, then substring."""
        rows = self.db.execute(
            "SELECT id, label, type, properties FROM entities WHERE label LIKE ? "
            "ORDER BY CASE WHEN label = ? THEN 0 WHEN label LIKE ? THEN 1 ELSE 2 END, label LIMIT ?",
            (f"%{query}%", query, f"{query}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def _traverse(self, entity_id: int, direction: str = "out", depth: int = 3,
                  rel_type: str | None = None) -> list[dict]:
        """Bounded graph traversal. UNION (not UNION ALL) plus a depth cap
        makes cycles terminate."""
        if direction not in ("out", "in"):
            raise ValueError("direction must be 'out' or 'in'")
        db = self.db
        depth = max(1, min(int(depth), 10))
        rel_filter = "AND r.type = ?" if rel_type else ""
        rel_params = [rel_type] if rel_type else []
        if direction == "out":
            base_next, base_where = "r.target_id", "r.source_id = ?"
            rec_join = "JOIN relations r ON r.source_id = p.next_id"
        else:
            base_next, base_where = "r.source_id", "r.target_id = ?"
            rec_join = "JOIN relations r ON r.target_id = p.next_id"
        sql = f"""
            WITH RECURSIVE path(rel_id, rel_type, next_id, lvl) AS (
                SELECT r.id, r.type, {base_next}, 1
                FROM relations r WHERE {base_where} {rel_filter}
                UNION
                SELECT r.id, r.type, {base_next}, p.lvl + 1
                FROM path p {rec_join}
                WHERE p.lvl < ? {rel_filter}
            )
            SELECT p.rel_id, p.rel_type AS relation, e.id AS entity_id, e.label, e.type AS entity_type, p.lvl AS level
            FROM path p JOIN entities e ON e.id = p.next_id
            ORDER BY p.lvl, p.rel_id
        """
        rows = db.execute(sql, [entity_id] + rel_params + [depth] + rel_params).fetchall()
        return [dict(r) for r in rows]

    def graph_explore(self, query: str, depth: int = 2, limit: int = 20) -> dict:
        """Entity lookup + outbound neighbors (drewgent graph-explore parity)."""
        out = {"query": query, "entities": []}
        for e in self._find_entities(query, limit):
            seen: set[tuple] = set()
            neighbors = []
            for n in self._traverse(e["id"], "out", depth):
                key = (n["relation"], n["entity_id"])
                if key in seen:
                    continue
                seen.add(key)
                neighbors.append(n)
            out["entities"].append({
                "id": e["id"], "label": e["label"], "type": e["type"],
                "neighbors": neighbors,
            })
        return out

    def graph_trace(self, query: str, depth: int = 4) -> dict:
        """Entity + bidirectional paths (drewgent graph-trace parity)."""
        out = {"query": query, "trace": []}
        for e in self._find_entities(query):
            out["trace"].append({
                "entity": e["label"], "type": e["type"],
                "inbound": self._traverse(e["id"], "in", depth),
                "outbound": self._traverse(e["id"], "out", depth),
            })
        return out

    def graph_rca(self, query: str, depth: int = 3) -> dict:
        """Root-cause analysis: incidents/patterns with their inbound `caused`
        chain and outbound `fixed_by` chain (drewgent graph-rca parity)."""
        out = {"query": query, "root_causes": []}
        for e in self._find_entities(query):
            if e["type"] not in ("incident", "pattern"):
                continue
            timeline = (
                [{"type": "cause", "entity": c["label"], "entity_type": c["entity_type"],
                  "level": c["level"]} for c in self._traverse(e["id"], "in", depth, rel_type="caused")]
                + [{"type": "fix", "entity": f["label"], "entity_type": f["entity_type"],
                    "level": f["level"]} for f in self._traverse(e["id"], "out", depth, rel_type="fixed_by")]
            )
            out["root_causes"].append({
                "incident": {"id": e["id"], "label": e["label"], "type": e["type"]},
                "timeline": timeline,
            })
        return out

    def transitive_closure(self, entity_id: int, rel_type: str = "depends_on",
                           direction: str = "out", depth: int = 5) -> list[dict]:
        """Chain of rel_type hops from an entity (inference.py transitive parity)."""
        return self._traverse(entity_id, direction, depth, rel_type=rel_type)

    # ── vault ingest (optional: rules.md → rules, incidents → episodes) ──
    def import_rules_md(self, path: str | Path) -> int:
        """Parse a drewgent-style rules markdown into the rules table.

        Per entry:
          ## [P0] rule text
          priority: N        (optional)
          scope: ...         (optional)
          condition: ...     (optional)
        Idempotent: identical (text, layer, priority) rules are skipped.
        """
        text = Path(path).read_text(encoding="utf-8")
        imported = 0
        for block in re.split(r"^## ", text, flags=re.M)[1:]:
            lines = block.splitlines()
            heading = lines[0].strip()
            m = re.match(r"\[(P[0-6])\]\s*(.*)", heading)
            layer = m.group(1) if m else "P0"
            rule_text = (m.group(2) if m else heading).strip()
            priority, scope, condition = 100, None, None
            for ln in lines[1:]:
                ln = ln.strip()
                mm = re.match(r"priority:\s*(\d+)", ln)
                if mm:
                    priority = int(mm.group(1))
                mm = re.match(r"scope:\s*(.+)", ln)
                if mm:
                    scope = mm.group(1)
                mm = re.match(r"condition:\s*(.+)", ln)
                if mm:
                    condition = mm.group(1)
            if not rule_text:
                continue
            exists = self.db.execute(
                "SELECT id FROM rules WHERE text = ? AND layer = ? AND priority = ? AND enabled = 1",
                (rule_text, layer, priority),
            ).fetchone()
            if exists is None:
                self.add_rule(rule_text, priority=priority, layer=layer, scope=scope,
                              condition=condition, source="import_rules_md")
                imported += 1
        return imported

    def import_incidents_dir(self, path: str | Path) -> int:
        """Read P6-prefrontal/incidents/*.md into episodes (kind='incident').
        Idempotent per file name."""
        imported = 0
        for f in sorted(Path(path).glob("*.md")):
            dup = self.db.execute(
                "SELECT id FROM episodes WHERE kind = 'incident' AND payload LIKE ? LIMIT 1",
                (f"%{f.name}%",),
            ).fetchone()
            if dup is not None:
                continue
            self.record_episode("incident", {"file": f.name, "content": f.read_text(encoding="utf-8")})
            imported += 1
        return imported

    # ── ops jobs: re-embed & consolidation ────────────────────
    def reembed(self, batch_size: int = 50, dry_run: bool = False) -> dict:
        """Re-embed active knowledge under the current embedder's version.

        Vectors are versioned: old versions stay in the table, recall only
        queries the current one, so a model switch never corrupts search.
        Idempotent — entries already embedded under the current version are
        skipped.
        """
        emb = self._get_embedder()
        if emb is None or not emb.available():
            return {"total": 0, "already": 0, "embedded": 0, "failed": 0,
                    "reason": "semantic embeddings unavailable"}
        db = self.db
        rows = db.execute(
            "SELECT k.id, k.content FROM knowledge k WHERE k.superseded_by IS NULL ORDER BY k.id"
        ).fetchall()
        have = {
            r["knowledge_id"]
            for r in db.execute(
                "SELECT knowledge_id FROM embeddings WHERE embedding_version = ?",
                (emb.embedding_version,),
            ).fetchall()
        }
        todo = [dict(r) for r in rows if r["id"] not in have]
        already = len(rows) - len(todo)
        embedded = 0
        failed = 0
        if not dry_run:
            for start in range(0, len(todo), batch_size):
                batch = todo[start:start + batch_size]
                try:
                    vecs = emb.embed([b["content"][:2000] for b in batch])
                except EmbeddingError:
                    failed += len(batch)
                    continue
                for b, vec in zip(batch, vecs):
                    if len(vec) != emb.dimensions:
                        failed += 1
                        continue
                    db.execute(
                        "INSERT OR REPLACE INTO embeddings "
                        "(knowledge_id, model, embedding_version, dimensions, vector, created_at) "
                        "VALUES (?,?,?,?,?,?)",
                        (b["id"], emb.model, emb.embedding_version, emb.dimensions, _vec_to_blob(vec), utcnow()),
                    )
                    embedded += 1
                db.commit()  # commit per batch — partial progress survives crashes
        return {"total": len(rows), "already": already, "embedded": embedded,
                "failed": failed, "dry_run": dry_run}

    def consolidate(self, min_episodes: int = 3, summarizer=None, dry_run: bool = False) -> dict:
        """Compress unconsolidated episodes into semantic-memory digests.

        Groups unconsolidated episodes by session (or kind), and writes one
        `insight` entry per group of `min_episodes` or more. The default
        summarizer is deterministic and offline; pass a callable
        summarizer(texts, group_key) -> str for an LLM digest. Idempotent:
        consolidated episodes are marked and never re-processed.
        """
        db = self.db
        rows = db.execute(
            "SELECT id, session_id, kind, payload FROM episodes "
            "WHERE consolidated_at IS NULL ORDER BY id"
        ).fetchall()
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            key = r["session_id"] or f"unattributed:{r['kind']}"
            groups[key].append(dict(r))

        digests = 0
        covered = 0
        skipped = 0
        for key, eps in sorted(groups.items()):
            if len(eps) < min_episodes:
                skipped += len(eps)
                continue
            texts = []
            for e in eps:
                raw = e["payload"]
                try:
                    payload = json.loads(raw) if raw.startswith("{") else raw
                except json.JSONDecodeError:
                    payload = raw
                if isinstance(payload, dict):
                    texts.append(
                        payload.get("title") or payload.get("file")
                        or payload.get("content") or json.dumps(payload, ensure_ascii=False)
                    )
                else:
                    texts.append(str(payload))
            if summarizer is not None:
                digest = summarizer(texts, key)
            else:
                unique = list(dict.fromkeys(texts))
                digest = f"[consolidated] {len(eps)} episode(s) [{key}]: " + " | ".join(unique[:4])[:500]
            if not dry_run:
                kid = self.add_knowledge(
                    digest,
                    type="insight",
                    layer="P5",
                    who="system:consolidation",
                    source=f"consolidation:{key}",
                    confidence=0.7,
                )
                marks = ",".join("?" for _ in eps)
                db.execute(
                    f"UPDATE episodes SET consolidated_at = ? WHERE id IN ({marks})",
                    (utcnow(), *[e["id"] for e in eps]),
                )
                db.commit()
                self._audit("consolidate", knowledge_id=kid, layer="P5", who="system:consolidation",
                            detail=f"group={key} episodes={len(eps)}")
                db.commit()
            digests += 1
            covered += len(eps)
        return {"digests": digests, "episodes_covered": covered, "skipped": skipped, "dry_run": dry_run}

    # ── drift report (read-only) ────────────────────────────────
    def drift_report(self, baseline_dir: str | os.PathLike | None = None,
                     ontology_path: str | os.PathLike | None = None,
                     proposals_dir: str | os.PathLike | None = None) -> dict:
        """Weekly drift report over the store's own data + P0 gate state.

        Metrics: knowledge/episodes/entities/relations/rules counts (7d + total)
        and the P0 ontology gate state (entries, open proposals). First run
        snapshots a baseline; later runs compare and flag metrics beyond
        ±DRIFT_PCT. Never modifies the store. Returns
        {"status": "no change"|"drift"|"failure", "path", "deltas", "metrics"}.
        """
        from . import gate as gate_mod

        dr = Path(baseline_dir) if baseline_dir else Path.cwd() / "p1-drift"
        dr.mkdir(parents=True, exist_ok=True)
        base_file = dr / "baseline.json"

        def count(sql: str, *args):
            try:
                return self.db.execute(sql, args).fetchone()[0]
            except sqlite3.Error:
                return None

        now_7d = "datetime('now','-7 days')"
        metrics = {
            "knowledge_total": count(f"SELECT COUNT(*) FROM knowledge"),
            "knowledge_7d": count(f"SELECT COUNT(*) FROM knowledge WHERE created_at >= {now_7d}"),
            "episodes_7d": count(f"SELECT COUNT(*) FROM episodes WHERE created_at >= {now_7d}"),
            "entities": count("SELECT COUNT(*) FROM entities"),
            "relations": count("SELECT COUNT(*) FROM relations"),
            "rules": count("SELECT COUNT(*) FROM rules"),
        }
        gf = gate_mod.fresh(Path(ontology_path) if ontology_path else None,
                            Path(proposals_dir) if proposals_dir else None)
        metrics["p0_entries"] = gf.get("entries")
        metrics["p0_open_proposals"] = gf.get("open_proposals")

        optional = {"p0_entries", "p0_open_proposals"}
        failures = [k for k, v in metrics.items() if v is None and k not in optional]
        if failures:
            return {"status": "failure", "metrics": metrics, "deltas": [],
                    "path": None, "unreadable": failures}

        baseline = {}
        try:
            if base_file.exists():
                d = json.loads(base_file.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    baseline = d.get("metrics", {})
        except (OSError, json.JSONDecodeError):
            baseline = {}

        def delta(cur, base):
            if cur is None or base is None:
                return None
            if not isinstance(cur, (int, float)) or not isinstance(base, (int, float)):
                return None
            if base == 0:
                return 100.0 if cur else 0.0
            return round(100.0 * (cur - base) / abs(base), 1)

        if not baseline:
            base_file.write_text(
                json.dumps({"created_at": utcnow(), "metrics": metrics}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            status, deltas = "no change", []
        else:
            deltas = [
                {"metric": k, "base": baseline.get(k), "cur": metrics.get(k), "delta_pct": delta(metrics.get(k), baseline.get(k))}
                for k in metrics
                if k in baseline and (d := delta(metrics.get(k), baseline.get(k))) is not None and abs(d) > 30
            ]
            status = "drift" if deltas else "no change"

        path = dr / f"weekly-{utcnow()[:10]}.md"
        lines = [f"# Drift Report — {utcnow()[:10]}", "",
                 f"**Status**: {'drift' if status == 'drift' else 'no change'}", "",
                 "| metric | baseline | current | delta |", "|---|---|---|---|"]
        for k in metrics:
            lines.append(f"| {k} | {baseline.get(k)} | {metrics.get(k)} | {delta(metrics.get(k), baseline.get(k))}% |")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {"status": status, "path": str(path), "deltas": deltas, "metrics": metrics}
