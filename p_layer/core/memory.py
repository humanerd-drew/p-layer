"""Memory business logic — ranked recall, serendipity, TTL management."""

import logging
import random
from datetime import datetime, timezone

_SERENDIPITY_CHANCE = 0.05
_DEFAULT_TTL = {"fact": 90, "decision": 180, "pattern": 30, "incident": 365}

logger = logging.getLogger(__name__)


def recall_ranked(db, query: str, limit: int = 10, layers: list = None,
                  serendipity: bool = True) -> list:
    """Recall with confidence-weighted ranking + serendipity.

    Falls back to standard search when Pg is unavailable (SQLite mode).
    """
    has_pg = getattr(db, '_pg_conn', None) is not None

    if not has_pg:
        results = db.search(query, layers=layers or ["P5", "P6"], limit=limit)
        return list(results) if results else []

    cur = db._pg_conn.cursor()

    where_parts = []
    params = []
    if layers:
        placeholders = ", ".join("%s" for _ in layers)
        where_parts.append(f"layer IN ({placeholders})")
        params.extend(layers)

    if query.strip():
        where_parts.append("tsv @@ plainto_tsquery('english', %s)")
        params.append(query)

    where_sql = " AND ".join(where_parts) if where_parts else "TRUE"
    ts_query = query.strip() or "."
    rank_expr = "ts_rank(tsv, plainto_tsquery('english', %s))"

    sql = f"""
        SELECT id, content, type, layer, confidence, ttl_days,
               access_count, last_accessed_at, version_id, created_at
        FROM entries
        WHERE {where_sql}
          AND (superseded_by IS NULL)
        ORDER BY
            ({rank_expr} * (0.5 + confidence * 0.5) *
             (1.0 + GREATEST(0.0, 1.0 - EXTRACT(EPOCH FROM (NOW() - created_at)) / 86400.0 /
                    NULLIF(NULLIF(ttl_days, 0), 0)) * 0.3)) DESC
        LIMIT %s
    """
    params.append(ts_query)
    params.append(limit)

    try:
        cur.execute(sql, params)
        rows = cur.fetchall()
    except Exception as e:
        logger.warning("Recall ranked query failed, falling back: %s", e)
        rows = db.search(query, layers=layers or ["P5", "P6"], limit=limit)
        rows = list(rows) if rows else []

    seen_ids = []
    results = []
    for row in rows:
        if isinstance(row, dict):
            results.append(row)
            if "id" in row:
                seen_ids.append(row["id"])
            continue
        entry = {
            "id": row[0], "content": row[1], "type": row[2],
            "layer": row[3], "confidence": row[4], "ttl_days": row[5],
            "access_count": row[6], "version_id": row[8],
        }
        seen_ids.append(row[0])
        results.append(entry)

    if seen_ids and has_pg:
        try:
            cur.execute(
                "UPDATE entries SET access_count = access_count + 1, "
                "last_accessed_at = NOW() WHERE id = ANY(%s)",
                (seen_ids,)
            )
            db._pg_conn.commit()
        except Exception:
            db._pg_conn.rollback()

    if serendipity and random.random() < _SERENDIPITY_CHANCE and seen_ids:
        try:
            cur.execute(
                "SELECT id, content, type, layer, confidence, ttl_days, "
                "access_count, version_id FROM entries "
                "WHERE id != ALL(%s) AND (superseded_by IS NULL) "
                "AND (access_count = 0 OR last_accessed_at IS NULL "
                "OR last_accessed_at < NOW() - INTERVAL '60 days') "
                "ORDER BY RANDOM() LIMIT 1",
                (seen_ids,)
            )
            wild = cur.fetchone()
            if wild:
                results.append({
                    "id": wild[0], "content": wild[1], "type": wild[2],
                    "layer": wild[3], "confidence": wild[4], "ttl_days": wild[5],
                    "access_count": wild[6], "version_id": wild[7],
                    "_serendipity": True,
                })
        except Exception:
            pass

    return results
