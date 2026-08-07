"""Semantic search on real data: does embeddings (ollama bge-m3) add value
over FTS+ILIKE for exact-match recall on the drewgent archive?

Finds: on this corpus (hyphenated Korean SEO slugs), bge-m3 semantic scores
do not align with exact-match expectations — FTS+ILIKE alone is already
optimal, and fusing semantic slightly dilutes it.

Run:  python3 benchmarks/semantic_real_data.py  (requires ollama + bge-m3)
Skips cleanly when ollama is unreachable.
"""
import os
import random
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from p_layer.eval import _drewgent_baseline_search, build_drewgent_baseline
from p_layer.import_drewgent import import_drewgent
from p_layer.store import Store

REAL_DB = os.path.expanduser(os.environ.get(
    "P_LAYER_BENCH_DB",
    "~/.drewgent/backups/archive/knowledge.db.20260803",
))
TOKEN_RE = re.compile(r"[a-z0-9가-힣]+")
STOP = {"the", "and", "for", "with", "from", "that", "this", "status", "http", "https", "ok", "seo", "b2b"}


def build_suite(rows, seed=7):
    rng, rng2 = random.Random(seed), random.Random(seed + 1)
    queries = []
    for r in rng.sample(rows, min(40, len(rows))):
        ts = []
        for w in TOKEN_RE.findall(r["content"].lower()):
            if len(w) >= 2 and w not in STOP and w not in ts:
                ts.append(w)
        if len(ts) >= 2:
            queries.append({"query": " ".join(ts[:2]), "expected": r["content"][:50]})
    return queries


def metrics(ranked_lists, expected):
    n = len(expected)
    h1 = sum(1 for r, e in zip(ranked_lists, expected) if e in r[0])
    h5 = sum(1 for r, e in zip(ranked_lists, expected) if any(e in c for c in r[:5]))
    mrr = sum(1.0 / (next(i + 1 for i, c in enumerate(r) if e in c))
              if any(e in c for c in r) else 0.0 for r, e in zip(ranked_lists, expected))
    return h1 / n, h5 / n, mrr / n


def main():
    try:
        from p_layer.embed import OllamaEmbedder

        probe = OllamaEmbedder(model="bge-m3", timeout=5)
        probe.embed(["ping"])
    except Exception as exc:
        print(f"ollama/bge-m3 unavailable ({exc}) — skipping")
        return

    tmp = tempfile.mkdtemp()
    s = Store(str(Path(tmp) / "m.db"), embedder=OllamaEmbedder(model="bge-m3", timeout=60))
    summary = import_drewgent(REAL_DB, s, reembed=True)
    print(f"import: {summary['knowledge_imported']} knowledge, "
          f"embeddings={s.stats()['embeddings']}")

    src = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    rows = [dict(r) for r in src.execute("SELECT id, content FROM knowledge WHERE content != ''").fetchall()]
    src.close()
    suite = build_suite(rows)
    expected = [q["expected"] for q in suite]

    ranked = {"baseline": [], "fts": [], "fts_sem": []}
    base = build_drewgent_baseline(s)
    base_content = {r["id"]: r["content"] for r in base.execute("SELECT id, content FROM knowledge").fetchall()}
    for q in suite:
        ids = _drewgent_baseline_search(base, q["query"], 5)
        ranked["baseline"].append([base_content.get(i, "") for i in ids])
        ranked["fts"].append([r["content"] for r in s.recall(q["query"], limit=5, use_semantic=False, serendipity=False)])
        ranked["fts_sem"].append([r["content"] for r in s.recall(q["query"], limit=5, use_semantic=True, serendipity=False)])
    base.close()

    print(f"\nqueries: {len(suite)} (token-pair, exact-match expected)")
    print(f"  {'':16}{'recall@1':>10}{'recall@5':>10}{'MRR':>10}")
    for name in ("baseline", "fts", "fts_sem"):
        h1, h5, mrr = metrics(ranked[name], expected)
        print(f"  {name:16}{h1:>10.3f}{h5:>10.3f}{mrr:>10.3f}")


if __name__ == "__main__":
    main()
