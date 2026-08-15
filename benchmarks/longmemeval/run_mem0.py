#!/usr/bin/env python3
"""Same-harness Mem0 comparison for LongMemEval-S.

Runs the identical LongMemEval retrieval protocol as
benchmarks/longmemeval/run_longmemeval.py against Mem0 OSS (in-process SDK):
  - per-question isolation via user_id scoping (Mem0 has no per-question DBs)
  - ingest: user turns only, session-granularity labels in metadata
  - recall: mem0 search (query, user_id, top_k)
  - metrics: session recall@k / MRR against answer_session_ids, turn
    recall@k via has_answer metadata — identical to the p-layer adapter

Honest configuration notes (recorded in README):
  - LLM = deepseek-v4-flash (OpenAI key on this machine is revoked/401);
    Mem0's retrieval quality depends on extraction quality, so the LLM choice
    matters and is pinned here.
  - Embedder = local ollama bge-m3; vector store = local Qdrant (Docker).
  - Subset runs by default: 500 questions x ~50 sessions = ~25k extraction
    LLM calls for a full run; --max-questions controls cost.

Usage:
  python3 run_mem0.py --data .../longmemeval_s_cleaned.json --max-questions 20 \
      --k 10 --out out/mem0
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", required=True)
    p.add_argument("--out", default="./mem0-out")
    p.add_argument("--k", type=int, default=10)
    p.add_argument("--max-questions", type=int, default=20)
    p.add_argument("--granularity", default="session", choices=["turn", "session"],
                   help="session = one add per session (fast, ~3.7s/add LLM extraction); "
                        "turn = per user turn (slow — full runs infeasible)")
    p.add_argument("--llm-model", default="deepseek-v4-flash")
    p.add_argument("--sleep", type=float, default=0.0)
    args = p.parse_args(argv)

    from mem0 import Memory

    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise SystemExit("DEEPSEEK_API_KEY required (deepseek provider for extraction)")
    config = {
        "llm": {"provider": "deepseek", "config": {"model": args.llm_model, "api_key": key}},
        "embedder": {"provider": "ollama",
                     "config": {"model": "bge-m3", "ollama_base_url": "http://localhost:11434"}},
        "vector_store": {"provider": "qdrant",
                         "config": {"host": "localhost", "port": 6333,
                                    "embedding_model_dims": 1024}},
    }
    mem = Memory.from_config(config)

    with open(args.data, encoding="utf-8") as fh:
        dataset = json.load(fh)
    items = dataset[: args.max_questions]
    print(f"[mem0] {len(items)} questions | k={args.k} llm={args.llm_model}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results = []
    done_path = out / "mem0_results.jsonl"
    done_ids = set()
    if done_path.exists():
        for line in done_path.open(encoding="utf-8"):
            line = line.strip()
            if line:
                row = json.loads(line)
                if "error" in row:
                    continue  # failed rows are re-tried
                done_ids.add(row["question_id"])
                results.append(row)
    results_by_id = {r["question_id"]: r for r in results}
    for qi, item in enumerate(items, start=1):
        qid = item["question_id"]
        if qid in done_ids:
            print(f"  [{qi}/{len(items)}] {qid} (cached)")
            continue
        uid = f"q-{qid}"
        t_q = time.time()
        # ingest: user turns only (turn) or whole session (session, default)
        answer_sids = set(item.get("answer_session_ids") or [])
        sids = item.get("haystack_session_ids") or []
        for si, (session, date) in enumerate(
                zip(item.get("haystack_sessions", []), item.get("haystack_dates") or [])):
            sid = sids[si] if si < len(sids) else f"session_{si + 1}"
            if args.granularity == "session":
                parts = []
                has_answer = False
                for turn in session:
                    content = turn.get("content", "")
                    if content.strip():
                        parts.append(content)
                    if turn.get("has_answer"):
                        has_answer = True
                if not parts:
                    continue
                mem.add("\n".join(parts), user_id=uid,
                        metadata={"session_id": sid, "has_answer": has_answer})
            else:
                for turn in session:
                    if turn.get("role") != "user":
                        continue
                    content = turn.get("content", "")
                    if not content.strip():
                        continue
                    mem.add(content, user_id=uid,
                            metadata={"session_id": sid,
                                      "has_answer": bool(turn.get("has_answer"))})
        if args.sleep:
            time.sleep(args.sleep)

        # recall
        hits_sid, hits_turn = [], []
        try:
            results_rows = mem.search(item["question"], top_k=args.k, filters={"user_id": uid})
        except Exception as exc:  # keep the run alive, record failure
            err_res = {"question_id": qid, "abstain": False, "error": str(exc)[:120],
                       "recall_at_k_sid": 0, "mrr_sid": 0.0, "recall_at_k_turn": 0,
                       "n_retrieved": 0, "n_hits_sid": 0, "n_hits_turn": 0}
            results.append(err_res)
            results_by_id[qid] = err_res
            print(f"  [{qi}/{len(items)}] {qid} ERROR {str(exc)[:80]}")
            continue
        for rank, row in enumerate(results_rows, start=1):
            if not isinstance(row, dict):
                continue
            meta = row.get("metadata") or {}
            if meta.get("session_id") in answer_sids:
                hits_sid.append(rank)
            if meta.get("has_answer"):
                hits_turn.append(rank)
        is_abs = "_abs" in qid
        res = {"question_id": qid,
               "question_type": item.get("question_type", ""),
               "abstain": is_abs,
               "recall_at_k_sid": 1 if hits_sid else 0,
               "mrr_sid": 1.0 / hits_sid[0] if hits_sid else 0.0,
               "recall_at_k_turn": 1 if hits_turn else 0,
               "n_retrieved": len(results_rows),
               "n_hits_sid": len(hits_sid),
               "n_hits_turn": len(hits_turn)}
        results.append(res)
        results_by_id[qid] = res
        status = "ABS" if is_abs else "ok"
        print(f"  [{qi}/{len(items)}] {qid} {status} R@k(sid)={res['recall_at_k_sid']} "
              f"R@k(turn)={res['recall_at_k_turn']} mrr={res['mrr_sid']:.3f} "
              f"hits={res['n_hits_sid']}/{res['n_retrieved']} ({time.time()-t_q:.0f}s)")
        with done_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(res, ensure_ascii=False) + "\n")

    scored = [r for r in results if not r["abstain"] and "error" not in r]
    n = len(scored) or 1
    summary = {
        "config": {"k": args.k, "llm_model": args.llm_model, "embedder": "ollama/bge-m3",
                   "vector_store": "qdrant", "granularity": args.granularity,
                   "questions": len(items)},
        "n_scored": len(scored),
        "n_abstain": sum(1 for r in results if r["abstain"]),
        "n_errors": sum(1 for r in results if "error" in r),
        "session_recall_at_k": sum(r["recall_at_k_sid"] for r in scored) / n,
        "turn_recall_at_k": sum(r["recall_at_k_turn"] for r in scored) / n,
        "session_mrr": sum(r["mrr_sid"] for r in scored) / n,
    }
    with open(out / "mem0_results.jsonl", "w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    results = list(results_by_id.values())
    with open(out / "summary.json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)
    print("\n[summary]")
    for key, val in summary.items():
        if isinstance(val, float):
            print(f"  {key}: {val:.4f}")
        else:
            print(f"  {key}: {val}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
