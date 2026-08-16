#!/usr/bin/env python3
"""Same-harness Basic Memory comparison for LongMemEval-S.

Runs the identical LongMemEval retrieval protocol against Basic Memory
(basicmachines-co, local-first markdown + SQLite + FastEmbed) — fully local,
zero API cost, no LLM anywhere in the loop:

  - one note per haystack session (whole session text, like the Mem0 pilot)
  - notes written as files directly, then `reset` + `reindex` once
  - per-question `tool search-notes` (hybrid FTS + vector), page_size = k
  - metrics: session recall@k / MRR vs answer_session_ids — identical to the
    p-layer and Mem0 adapters

Usage (requires the basic-memory CLI on PATH and a project at BM_FOLDER):
  BM_FOLDER=/tmp/bm-bench/bench BM_VENV=/tmp/bm-venv/bin/basic-memory \
    python3 run_basic_memory.py --data .../longmemeval_s_cleaned.json \
    --max-questions 20 --k 10 --out out/basic-memory
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str], stdin: str | None = None) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, input=stdin)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {r.stderr[-300:]}")
    return r.stdout


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", required=True)
    p.add_argument("--out", default="./bm-out")
    p.add_argument("--k", type=int, default=10)
    p.add_argument("--max-questions", type=int, default=20)
    args = p.parse_args(argv)

    bm = os.environ.get("BM_VENV", "basic-memory")
    folder = Path(os.environ.get("BM_FOLDER", "/tmp/bm-bench/bench"))
    notes_dir = folder / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    # clean previous run
    for f in notes_dir.glob("*.md"):
        f.unlink()

    with open(args.data, encoding="utf-8") as fh:
        dataset = json.load(fh)
    items = dataset[: args.max_questions]

    # title -> session id
    title_to_meta: dict[str, dict] = {}
    for qi, item in enumerate(items, start=1):
        sids = item.get("haystack_session_ids") or []
        for si, session in enumerate(item.get("haystack_sessions", [])):
            parts = [t.get("content", "") for t in session if t.get("content", "").strip()]
            if not parts:
                continue
            title = f"q{qi}-s{si + 1}"
            sid = sids[si] if si < len(sids) else f"session_{si + 1}"
            body = "\n\n".join(parts)
            notes_dir.joinpath(f"{title}.md").write_text(
                f"---\ntitle: {title}\ntype: note\npermalink: bench/notes/{title}\n---\n\n{body}",
                encoding="utf-8",
            )
            title_to_meta[title] = {"session_id": sid}
    print(f"[basic-memory] {len(items)} questions, {len(title_to_meta)} notes | k={args.k}")

    run([bm, "reset", "--force"], stdin="y\n")
    run([bm, "reindex"])

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results = []
    for qi, item in enumerate(items, start=1):
        qid = item["question_id"]
        answer_sids = set(item.get("answer_session_ids") or [])
        raw = run([bm, "tool", "search-notes", item["question"], "--page-size", str(args.k)])
        try:
            data = json.loads(raw)
            rows = data.get("results", [])
        except json.JSONDecodeError:
            rows = []
        hits = []
        for rank, r in enumerate(rows, start=1):
            title = r.get("title", "")
            meta = title_to_meta.get(title, {})
            if meta.get("session_id") in answer_sids:
                hits.append(rank)
        is_abs = "_abs" in qid
        res = {"question_id": qid, "abstain": is_abs,
               "recall_at_k_sid": 1 if hits else 0,
               "mrr_sid": 1.0 / hits[0] if hits else 0.0,
               "n_retrieved": len(rows), "n_hits_sid": len(hits)}
        results.append(res)
        print(f"  [{qi}/{len(items)}] {qid} {'ABS' if is_abs else 'ok'} "
              f"R@k(sid)={res['recall_at_k_sid']} mrr={res['mrr_sid']:.3f} "
              f"hits={res['n_hits_sid']}/{res['n_retrieved']}")

    scored = [r for r in results if not r["abstain"]]
    n = len(scored) or 1
    summary = {
        "config": {"k": args.k, "system": "basic-memory (local, no LLM)",
                   "questions": len(items)},
        "n_scored": len(scored),
        "session_recall_at_k": sum(r["recall_at_k_sid"] for r in scored) / n,
        "session_mrr": sum(r["mrr_sid"] for r in scored) / n,
    }
    with open(out / "bm_results.jsonl", "w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(out / "summary.json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)
    print(f"[summary] session recall@{args.k}: {summary['session_recall_at_k']:.4f} "
          f"| mrr: {summary['session_mrr']:.4f} | n={len(scored)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
