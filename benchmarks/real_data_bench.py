"""Real-data benchmark: import the live drewgent memory, then measure
retrieval quality (recall@k / MRR vs the drewgent baseline) and store
performance (bulk write + query latency at scale).

Run:  python3 benchmarks/real_data_bench.py
"""
import json
import os
import random
import re
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from p_layer.embed import NoopEmbedder
from p_layer.eval import _drewgent_baseline_search, build_drewgent_baseline
from p_layer.import_drewgent import import_drewgent
from p_layer.store import Store

REAL_DB = os.path.expanduser(os.environ.get(
    "P_LAYER_BENCH_DB",
    "~/.drewgent/backups/archive/knowledge.db.20260803",  # the author's drewgent memory archive
))
TOKEN_RE = re.compile(r"[a-z0-9가-힣]+")
STOP = {"the", "and", "for", "with", "from", "that", "this", "status", "http",
        "https", "ok", "seo", "b2b", "마케팅전략"}


def tokens(content: str) -> list[str]:
    seen = []
    for w in TOKEN_RE.findall(content.lower()):
        if len(w) >= 2 and w not in STOP and w not in seen:
            seen.append(w)
    return seen


def make_suite(db_path: str, seed: int = 7) -> dict:
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    rows = [dict(r) for r in src.execute(
        "SELECT id, content FROM knowledge WHERE content IS NOT NULL AND content != ''").fetchall()]
    src.close()
    rng = random.Random(seed)
    sample = rng.sample(rows, min(40, len(rows)))
    queries = []
    for r in sample:
        ts = tokens(r["content"])
        if len(ts) >= 2:
            queries.append({"query": " ".join(ts[:2]), "expected": [r["content"][:50]], "k": 5})
    # hard case: concatenated CJK compounds (no separator) — FTS boundary stress
    hard = []
    for r in rows:
        ts = tokens(r["content"])
        for i in range(len(ts) - 1):
            compound = ts[i] + ts[i + 1]
            if re.fullmatch(r"[가-힣]{4,}", compound):
                hard.append({"query": compound, "expected": [r["content"][:50]], "k": 5})
                break
        if len(hard) >= 10:
            break
    return {"queries": queries + hard}


def recall_mrr(store, baseline, suite, k):
    base_hits1 = base_hits5 = mem_hits1 = mem_hits5 = 0
    base_rrs = mem_rrs = 0.0
    n = 0
    base_content = {r["id"]: r["content"] for r in baseline.execute("SELECT id, content FROM knowledge").fetchall()}
    for q in suite["queries"]:
        exp = q["expected"][0]
        n += 1
        base_ids = _drewgent_baseline_search(baseline, q["query"], k)
        base_ranked = [base_content.get(i, "") for i in base_ids]
        mem = store.recall(q["query"], limit=k, serendipity=False)
        mem_ranked = [r["content"] for r in mem]
        for label, ranked in (("base", base_ranked), ("mem", mem_ranked)):
            pos = next((i + 1 for i, c in enumerate(ranked) if exp in c), None)
            if label == "base":
                base_rrs += 1.0 / pos if pos else 0.0
                base_hits1 += pos == 1
                base_hits5 += pos is not None
            else:
                mem_rrs += 1.0 / pos if pos else 0.0
                mem_hits1 += pos == 1
                mem_hits5 += pos is not None
    return {
        "queries": n,
        "base": {"recall@1": base_hits1 / n, "recall@5": base_hits5 / n, "mrr": base_rrs / n},
        "p_layer": {"recall@1": mem_hits1 / n, "recall@5": mem_hits5 / n, "mrr": mem_rrs / n},
    }


def perf_benchmark():
    tmp = tempfile.mkdtemp()
    s = Store(str(Path(tmp) / "perf.db"), embedder=NoopEmbedder())
    results = {}
    for n in (1000, 5000):
        t0 = time.perf_counter()
        for i in range(n):
            s.add_knowledge(
                f"마케팅 전략 문서 {i}: b2b 세그먼트 분석과 채널별 성과 측정 방법 {i}",
                type="fact", source="bench",
            )
        dt = time.perf_counter() - t0
        base = build_drewgent_baseline(s)
        lat_p, lat_b = [], []
        for i in range(50):
            q = f"b2b 세그먼트 {i % n}"
            t1 = time.perf_counter()
            s.recall(q, limit=5, serendipity=False)
            lat_p.append((time.perf_counter() - t1) * 1000)
            t1 = time.perf_counter()
            _drewgent_baseline_search(base, q, 5)
            lat_b.append((time.perf_counter() - t1) * 1000)
        base.close()
        results[n] = {
            "insert_ms_per_op": round(dt / n * 1000, 2),
            "p_layer_recall_p50_ms": round(statistics.median(lat_p), 2),
            "p_layer_recall_p95_ms": round(sorted(lat_p)[int(len(lat_p) * 0.95)], 2),
            "baseline_search_p50_ms": round(statistics.median(lat_b), 2),
            "baseline_search_p95_ms": round(sorted(lat_b)[int(len(lat_b) * 0.95)], 2),
        }
    s.close()
    return results


def main():
    print("=" * 60)
    print("REAL-DATA BENCHMARK — drewgent memory -> p-layer")
    print("=" * 60)

    tmp = tempfile.mkdtemp()
    store = Store(str(Path(tmp) / "imported.db"), embedder=NoopEmbedder())
    t0 = time.perf_counter()
    summary = import_drewgent(REAL_DB, store, reembed=False)
    dt = time.perf_counter() - t0
    print(f"\n[import] {os.path.basename(REAL_DB)}")
    print(f"  {summary['knowledge_imported']} knowledge, "
          f"{summary['entities_imported']} entities, "
          f"{summary['relations_imported']} relations, "
          f"{summary['sessions_imported']} sessions  ({dt*1000:.0f} ms)")
    stats = store.stats()
    print(f"  store: {stats['knowledge']} knowledge / {stats['by_type']}")

    suite = make_suite(REAL_DB)
    print(f"\n[eval] {len(suite['queries'])} queries on real data "
          f"({sum(1 for q in suite['queries'] if len(q['query'].replace(' ','')) < 5)} CJK-compound hard cases)")
    baseline = build_drewgent_baseline(store)
    res = recall_mrr(store, baseline, suite, k=5)
    baseline.close()
    print(f"  {'':12}{'recall@1':>10}{'recall@5':>10}{'MRR':>10}")
    print(f"  {'drewgent':12}{res['base']['recall@1']:>10.3f}{res['base']['recall@5']:>10.3f}{res['base']['mrr']:>10.3f}")
    print(f"  {'p-layer':12}{res['p_layer']['recall@1']:>10.3f}{res['p_layer']['recall@5']:>10.3f}{res['p_layer']['mrr']:>10.3f}")

    print("\n[perf] SQLite (WAL) — bulk insert + query latency (p-layer vs drewgent baseline)")
    perf = perf_benchmark()
    print(f"  {'size':>8}{'insert ms/op':>14}{'p-layer p50':>14}{'p-layer p95':>14}{'baseline p50':>15}{'baseline p95':>15}")
    for n, r in perf.items():
        print(f"  {n:>8}{r['insert_ms_per_op']:>14}{r['p_layer_recall_p50_ms']:>14}"
              f"{r['p_layer_recall_p95_ms']:>14}{r['baseline_search_p50_ms']:>15}{r['baseline_search_p95_ms']:>15}")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()
