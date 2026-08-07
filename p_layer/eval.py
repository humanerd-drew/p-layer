"""Evaluation harness — proves the governance thesis with numbers.

Two engines, same data:

  baseline  — drewgent's knowledge-db.ts recall path (naive FTS5 OR-join,
              quote-stripped, no confidence/freshness/diversification).
  p_layer   — hybrid FTS5+semantic RRF fusion, confidence & freshness ranked,
              superseded excluded, type-diversified.

`p_layer eval <suite.json>` reports recall@k for both plus ACL compliance:
the share of (layer, who) enforcement cases the store gets right. That is the
"governance, not just retrieval" evidence: writes are denied in code, and
ranking beats the naive baseline.

Suite format:
  {
    "queries": [
      {"query": "portone payment", "expected": ["switched to portone v2"], "k": 5},
      ...
    ]
  }
expected entries are substrings that must appear in the top-k results.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .store import LAYER_WRITERS, Store, WriteDenied, _check_layer_write


def load_suite(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data.get("queries"), list) or not data["queries"]:
        raise ValueError("suite must contain a non-empty 'queries' list")
    for q in data["queries"]:
        if not q.get("query") or not isinstance(q.get("expected"), list) or not q["expected"]:
            raise ValueError(f"each query needs 'query' and a non-empty 'expected' list: {q}")
    return data


def _drewgent_baseline_search(db: sqlite3.Connection, query: str, limit: int) -> list[int]:
    """Replicates drewgent's searchKnowledge(): strip quotes, OR-join whitespace.
    Arbitrary user strings can break FTS5 syntax (drewgent's TS tool crashes on
    them); the benchmark treats those as empty results."""
    safe = query.replace("'", "").replace('"', "").replace(" ", " OR ")
    try:
        rows = db.execute(
            "SELECT k.id FROM knowledge_fts f JOIN knowledge k ON k.id = f.rowid "
            "WHERE knowledge_fts MATCH ? ORDER BY rank LIMIT ?",
            (safe, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [r[0] for r in rows]


def build_drewgent_baseline(store: Store) -> sqlite3.Connection:
    """Copy active knowledge rows into a drewgent-schema in-memory DB (same data,
    the baseline engine)."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE knowledge (id INTEGER PRIMARY KEY, type TEXT, content TEXT, source TEXT, created_at TEXT)")
    db.execute("CREATE VIRTUAL TABLE knowledge_fts USING fts5(content, type)")
    rows = store.db.execute(
        "SELECT id, type, content, source, created_at FROM knowledge "
        "WHERE superseded_by IS NULL ORDER BY id"
    ).fetchall()
    for r in rows:
        db.execute("INSERT INTO knowledge VALUES (?,?,?,?,?)", tuple(r))
        db.execute("INSERT INTO knowledge_fts (rowid, content, type) VALUES (?,?,?)", (r["id"], r["content"], r["type"]))
    db.commit()
    return db


def recall_at_k(store: Store, baseline: sqlite3.Connection, suite: dict) -> dict:
    results: dict[str, dict] = {"baseline": {}, "p_layer": {}}
    totals = {"baseline": {"hits": 0, "expected": 0}, "p_layer": {"hits": 0, "expected": 0}}
    baseline_contents = {
        r["id"]: r["content"]
        for r in baseline.execute("SELECT id, content FROM knowledge").fetchall()
    }
    for q in suite["queries"]:
        k = int(q.get("k", 5))
        expected = [e.lower() for e in q["expected"]]
        totals["baseline"]["expected"] += len(expected)
        totals["p_layer"]["expected"] += len(expected)

        base_ids = _drewgent_baseline_search(baseline, q["query"], k)
        base_hits = sum(1 for e in expected if any(e in baseline_contents.get(i, "").lower() for i in base_ids))
        totals["baseline"]["hits"] += base_hits

        mem = store.recall(q["query"], limit=k, serendipity=False)
        mem_hits = sum(1 for e in expected if any(e in r["content"].lower() for r in mem))
        totals["p_layer"]["hits"] += mem_hits

        results["baseline"][q["query"]] = {"recall@k": base_hits / len(expected), "hits": base_hits, "expected": len(expected)}
        results["p_layer"][q["query"]] = {"recall@k": mem_hits / len(expected), "hits": mem_hits, "expected": len(expected)}

    for engine in ("baseline", "p_layer"):
        t = totals[engine]
        results[engine]["_total"] = {
            "recall@k": (t["hits"] / t["expected"]) if t["expected"] else 0.0,
            "hits": t["hits"],
            "expected": t["expected"],
        }
    return results


def acl_compliance() -> dict:
    """Every (layer, allowed_who) must pass; every layer must deny a stranger;
    an invalid layer must raise ValueError. 100% is the compliance baseline."""
    total = 0
    passed = 0
    cases: list[dict] = []
    known = {"system", "cron", "gateway", "agent", "manual", "tool", "human"}
    for layer, allowed in sorted(LAYER_WRITERS.items()):
        for who in sorted(allowed):
            total += 1
            try:
                _check_layer_write(layer, who)
                ok = True
            except Exception:
                ok = False
            passed += ok
            cases.append({"layer": layer, "who": who, "expect": "allow", "ok": ok})
        stranger = next(iter(known - allowed), "stranger")
        total += 1
        try:
            _check_layer_write(layer, stranger)
            ok = False  # should have been denied
        except WriteDenied:
            ok = True
        except Exception:
            ok = False
        passed += ok
        cases.append({"layer": layer, "who": stranger, "expect": "deny", "ok": ok})
    total += 1
    try:
        _check_layer_write("P99", "system")
        ok = False
    except ValueError:
        ok = True
    except Exception:
        ok = False
    passed += ok
    cases.append({"layer": "P99", "who": "system", "expect": "invalid_layer", "ok": ok})
    return {
        "pass_rate": round(passed / total, 4) if total else 1.0,
        "passed": passed,
        "total": total,
        "cases": cases,
    }


def run_eval(store: Store, suite: dict) -> dict:
    baseline = build_drewgent_baseline(store)
    try:
        recall = recall_at_k(store, baseline, suite)
    finally:
        baseline.close()
    return {"recall": recall, "acl": acl_compliance()}


def format_report(result: dict) -> str:
    recall = result["recall"]
    acl = result["acl"]
    b, m = recall["baseline"]["_total"], recall["p_layer"]["_total"]
    lines = [
        "recall@k (same data, two engines):",
        f"  drewgent baseline : {b['recall@k']:.3f} ({b['hits']}/{b['expected']})",
        f"  p-layer            : {m['recall@k']:.3f} ({m['hits']}/{m['expected']})",
        f"  delta             : {m['recall@k'] - b['recall@k']:+.3f}",
        "",
        f"ACL compliance: {acl['pass_rate']:.1%} ({acl['passed']}/{acl['total']}) enforcement cases correct",
    ]
    return "\n".join(lines)
