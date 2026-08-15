#!/usr/bin/env python3
"""LongMemEval adapter for p-layer.

Runs the LongMemEval benchmark (CMU, ICLR 2025) against p-layer:
  ingest  -> per-question isolated Store, haystack sessions written as
             governed knowledge entries (default layer P2 / system)
  recall  -> p-layer hybrid recall (FTS5 + semantic, RRF-fused) on the
             *shipped* Store path
  metrics -> session-level recall@k / MRR vs answer_session_ids, plus
             turn-level recall@k using has_answer labels (abstention
             questions reported separately, never folded into the score)
  [qa]    -> optional answer generation via any OpenAI-compatible API,
             writing hypothesis.jsonl in the official LongMemEval format
             (judge with the official repo's evaluate_qa.py afterwards)

Zero runtime dependencies on purpose: only stdlib + the p_layer package.
Reproducibility contract (see benchmarks/longmemeval/README.md):
  - one fresh DB per question (cross-question leakage is the #1 invalidator)
  - pinned embedder via --embedder (hash is fully offline & deterministic)
  - retrieval-only numbers are reported separately from QA accuracy, never
    as a substitute (see the MeMesh adapter lesson in the playbook)

Usage:
  python3 run_longmemeval.py --data longmemeval_s_cleaned.json \
      --k 10 --embedder hash --max-questions 5 --out ./out

  GPT API key rule (absolute): the OpenAI API key may only call gpt-5.6-luna.
  The QA lane's official harness contract is gpt-4o (reader + official judge).
  Since gpt-4o is NOT available on the GPT key, the QA lane is REFUSED —
  the model is never substituted. Run the QA lane only with a key that
  actually has the required model, or skip it (retrieval-only is offline).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# Make p_layer importable when running from a repo checkout (PYTHONPATH=repo)
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from p_layer.embed import load_embedder  # noqa: E402
from p_layer.store import Store  # noqa: E402

DEFAULT_LAYER = "P2"  # raw sessions live in P2 (system may write)
DEFAULT_WHO = "system"


def _norm_date(value: str | None) -> str | None:
    """Normalize LongMemEval dates ("2023/05/20 (Sat) 02:21") to ISO so
    lexicographic sort == chronological sort for the read stage."""
    if not value:
        return None
    m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})\D*([0-9:]*)", value.strip())
    if not m:
        return value
    y, mo, d, hhmm = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
    time_part = f"T{hhmm}" if hhmm else ""
    return f"{y}-{mo:02d}-{d:02d}{time_part}"


# --------------------------------------------------------------------------
# Ingest
# --------------------------------------------------------------------------

def ingest_question(store: Store, item: dict, layer: str, granularity: str, roles: str = "user") -> dict:
    """Write a question's haystack into the store. Returns mapping
    knowledge_id -> {session_id, has_answer} for metric computation.

    granularity="turn":    one knowledge row per turn
    granularity="session": one knowledge row per session (content joined)
    roles="user"|"all":   official LongMemEval retrieval uses user-side
        utterances as keys only; "all" (user+assistant) matches real p-layer
        usage and the paper's read-stage values (full rounds).
    """
    idx = {}
    sessions = item.get("haystack_sessions", [])
    dates = item.get("haystack_dates") or [None] * len(sessions)
    sids = item.get("haystack_session_ids") or [f"session_{si + 1}" for si in range(len(sessions))]
    # Official format: sessions are turn-lists; their ids are the real
    # haystack_session_ids, which answer_session_ids references directly
    # (answer sessions carry ids starting with "answer", e.g. answer_280352e9).
    for si, (session, date) in enumerate(zip(sessions, dates)):
        sid = sids[si] if si < len(sids) else f"session_{si + 1}"
        if granularity == "session":
            content = "\n".join(
                f"{t.get('role', '?')}: {t.get('content', '')}" for t in session
            )
            has_answer = any(t.get("has_answer") for t in session)
            kid = store.add_knowledge(
                content=content,
                type="fact",
                session_id=sid,
                created_at=_norm_date(date) or None,
                layer=layer,
                who=DEFAULT_WHO,
            )
            idx[kid] = {"session_id": sid, "has_answer": has_answer, "role": "any"}
            continue
        # turn granularity — official retrieval corpus is user turns only;
        # roles="all" stores assistant turns too (real p-layer usage, and the
        # paper's read-stage values are full rounds).
        for turn in session:
            if roles == "user" and turn.get("role") != "user":
                continue
            content = turn.get("content", "")
            if not content.strip():
                continue
            kid = store.add_knowledge(
                content=content,
                type="fact",
                session_id=sid,
                created_at=_norm_date(date) or None,
                layer=layer,
                who=DEFAULT_WHO,
            )
            idx[kid] = {
                "session_id": sid,
                "has_answer": bool(turn.get("has_answer")),
                "role": turn.get("role", "user"),
            }
    return idx


# --------------------------------------------------------------------------
# Retrieval metrics
# --------------------------------------------------------------------------

def _bm25_rank(store: Store, query: str, k: int) -> list[dict]:
    """Pure-Python Okapi BM25 (k1=1.2, b=0.75) over the same knowledge corpus
    the default run indexes — a same-harness sparse baseline (the official
    flat-bm25 uses pyserini; this is metric-equivalent, dependency-free)."""
    import math
    from collections import Counter

    docs = [dict(r) for r in store.db.execute(
        "SELECT id, content FROM knowledge WHERE superseded_by IS NULL").fetchall()]
    if not docs:
        return []
    tokenize = lambda s: re.findall(r"[a-z0-9가-힣]+", s.lower())
    doc_tokens = [tokenize(d["content"]) for d in docs]
    N = len(docs)
    df = Counter()
    for toks in doc_tokens:
        for t in set(toks):
            df[t] += 1
    avgdl = sum(len(t) for t in doc_tokens) / N
    k1, b = 1.2, 0.75
    q_terms = set(tokenize(query))
    if not q_terms:
        return []
    scored = []
    for i, toks in enumerate(doc_tokens):
        if not toks:
            continue
        tf = Counter(toks)
        dl = len(toks)
        score = 0.0
        for t in q_terms:
            if t not in tf:
                continue
            idf = math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5))
            score += idf * (tf[t] * (k1 + 1)) / (tf[t] + k1 * (1 - b + b * dl / avgdl))
        scored.append((score, i))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [docs[i] for _, i in scored[:k]]


def evaluate_retrieval(store: Store, item: dict, idx: dict, k: int, baseline: str = "hybrid") -> dict:
    """Recall@k / MRR against LongMemEval labels. Abstention questions have
    no evidence sessions -> returned with abstain=True and never averaged in."""
    answer_sids = set(item.get("answer_session_ids") or [])
    # Official abstention rule: question ids carry the "_abs" suffix (30 of
    # 500). Their answer sessions (answer_*_abs_N) are still scored for
    # retrieval but excluded from the aggregated headline numbers.
    is_abs = "_abs" in item["question_id"]
    if baseline == "fts":
        rows = [dict(r) for r in store.fts_search(item["question"], limit=k)]
    elif baseline == "bm25":
        rows = _bm25_rank(store, item["question"], k)
    else:
        rows = store.recall(item["question"], limit=k)
    hits_sid = []
    hits_turn = []
    for rank, row in enumerate(rows, start=1):
        meta = idx.get(row["id"], {})
        if meta.get("session_id") in answer_sids:
            hits_sid.append(rank)
        # Official turn-level ground truth is user turns carrying has_answer.
        if meta.get("role") == "user" and meta.get("has_answer"):
            hits_turn.append(rank)
    return {
        "question_id": item["question_id"],
        "question_type": item.get("question_type", ""),
        "abstain": is_abs,
        "recall_at_k_sid": 1 if hits_sid else 0,
        "mrr_sid": 1.0 / hits_sid[0] if hits_sid else 0.0,
        "recall_at_k_turn": 1 if hits_turn else 0,
        "n_retrieved": len(rows),
        "n_hits_sid": len(hits_sid),
        "n_hits_turn": len(hits_turn),
    }


# --------------------------------------------------------------------------
# Optional QA generation (OpenAI-compatible HTTP; no SDK dependency)
# --------------------------------------------------------------------------

def chat_once(model: str, query: str, context: str, con: bool = False) -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("--generate requires OPENAI_API_KEY")
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    # Absolute rule: the GPT API key may only call gpt-5.6-luna. No model
    # substitution — if the harness needs another model, the run is refused.
    if base == "https://api.openai.com/v1" and model != "gpt-5.6-luna":
        raise SystemExit(
            f"REFUSED: {model} is not available on the GPT API key "
            f"(luna-only rule). The model is never substituted. "
            f"Use a key that has {model}, or drop this call."
        )
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are being evaluated on long-term memory. Answer the "
                        "question using ONLY the retrieved memory below. If the "
                        "answer cannot be determined from it, reply exactly: "
                        "I don't know."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        build_con_prompt(query, context)
                        if con
                        else f"Retrieved memory:\n{context or '(none)'}\n\nQuestion: {query}"
                    ),
                },
            ],
            "temperature": 0,
        }
    ).encode("utf-8")
    last_err = None
    for attempt in range(8):
        req = urllib.request.Request(
            f"{base}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as exc:
            last_err = exc
            if exc.code in (429, 500, 502, 503, 504):
                time.sleep(min(2 ** attempt, 60))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_err = exc
            time.sleep(min(2 ** attempt, 60))
    raise RuntimeError(f"chat_once failed after retries: {last_err}")


def build_context(rows: list[dict]) -> str:
    # Chronological order (paper's read-stage design: retrieved items sorted
    # by timestamp). ISO-normalized created_at sorts lexicographically ==
    # chronologically; rows without a timestamp sort last.
    rows = sorted(rows, key=lambda r: (r.get("created_at") or ""))
    parts = []
    for r in rows:
        parts.append(f"[{r.get('layer', '?')}|conf={r.get('confidence', '?')}] {r['content']}")
    return "\n".join(parts)


def build_con_prompt(query: str, context: str) -> str:
    """Chain-of-Note reading: extract facts first, then answer (the paper's
    strongest reading strategy, worth up to 10 absolute QA points)."""
    return (
        "You are being evaluated on long-term memory. Read the memory items "
        "below. First list the relevant facts each item provides (in notes), "
        "then answer the question based ONLY on those facts. If the answer "
        "cannot be determined, reply exactly: I don't know.\n\n"
        f"Memory items:\n{context or '(none)'}\n\nQuestion: {query}"
    )


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", required=True, help="longmemeval_*_cleaned.json")
    p.add_argument("--out", default="./longmemeval-out", help="output directory")
    p.add_argument("--k", type=int, default=10, help="recall limit")
    p.add_argument("--baseline", default="hybrid", choices=["hybrid", "fts", "bm25"],
                   help="hybrid = p-layer recall (FTS+semantic RRF); fts = FTS-only; "
                        "bm25 = pure-Python Okapi BM25 over the same corpus")
    p.add_argument("--embedder", default="hash", choices=["hash", "ollama", "none"])
    p.add_argument("--layer", default=DEFAULT_LAYER, help="governance layer for ingest")
    p.add_argument("--granularity", default="turn", choices=["turn", "session"])
    p.add_argument("--roles", default="user", choices=["user", "all"],
                   help="which turns to ingest: user (official retrieval keys) "
                        "or all (user+assistant, real usage / read-stage values)")
    p.add_argument("--max-questions", type=int, default=0, help="0 = all")
    p.add_argument("--generate", action="store_true", help="also produce hypothesis.jsonl")
    p.add_argument("--model", default=None,
                   help="generation model. The official QA lane is gpt-4o by harness contract; "
                        "the GPT API key is luna-only, so --generate without an explicit, "
                        "key-available --model is REFUSED (no substitution).")
    p.add_argument("--read-strategy", default="direct", choices=["direct", "con"],
                   help="direct = answer from retrieved memory; con = Chain-of-Note "
                        "(facts first, then answer) + chronological order")
    p.add_argument("--sleep", type=float, default=0.0, help="seconds between questions (rate limit)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # QA lane refusal: the official harness contract is gpt-4o; the GPT API key
    # is luna-only and gpt-4o is not on it. Refuse rather than substitute.
    if args.generate and not args.model:
        raise SystemExit(
            "REFUSED: --generate requires an explicit --model. The QA lane's official "
            "harness contract is gpt-4o, which is not available on the GPT API key "
            "(luna-only rule). The model is never substituted — run the QA lane only "
            "with a key that has the required model, or skip --generate."
        )
    if args.generate and args.model != "gpt-5.6-luna":
        raise SystemExit(
            f"REFUSED: {args.model} is not available on the GPT API key (luna-only "
            f"rule). The model is never substituted — use a key that has it, or skip "
            f"--generate."
        )
    with open(args.data, encoding="utf-8") as fh:
        dataset = json.load(fh)
    items = dataset if isinstance(dataset, list) else dataset.get("data", dataset.get("instances", []))
    if args.max_questions:
        items = items[: args.max_questions]
    print(f"[longmemeval] {len(items)} questions | k={args.k} embedder={args.embedder} "
          f"baseline={args.baseline} layer={args.layer} granularity={args.granularity} roles={args.roles}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="longmemeval-"))
    embedder = load_embedder(args.embedder)

    results: list[dict] = []
    hypotheses: list[dict] = []
    done_ids: set[str] = set()
    hypo_path = out / "hypothesis.jsonl"
    if args.generate and hypo_path.exists():
        # checkpoint-resume: skip questions already answered
        for line in hypo_path.open(encoding="utf-8"):
            line = line.strip()
            if line:
                done_ids.add(json.loads(line)["question_id"])
    # Retrieval checkpointing too: retrieval runs can take hours with a slow
    # embedder (e.g. ollama), so completed rows are appended incrementally and
    # re-runs skip what is already scored.
    retr_path = out / "retrieval_results.jsonl"
    results_by_id: dict[str, dict] = {}
    if retr_path.exists():
        for line in retr_path.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            results_by_id[row["question_id"]] = row
    hypo_fh = hypo_path.open("a", encoding="utf-8") if args.generate else None
    try:
        for qi, item in enumerate(items, start=1):
            qid = item["question_id"]
            if qid in results_by_id:
                res = results_by_id[qid]
                print(f"  [{qi}/{len(items)}] {qid} (cached)")
                continue
            db_path = tmp / f"q{qi}.db"
            store = Store(db_path, embedder=embedder)
            try:
                idx = ingest_question(store, item, args.layer, args.granularity, args.roles)
                res = evaluate_retrieval(store, item, idx, args.k, args.baseline)
                results_by_id[qid] = res
                if args.generate and qid not in done_ids:
                    rows = store.recall(item["question"], limit=args.k)
                    hypo = {"question_id": qid,
                            "hypothesis": chat_once(args.model, item["question"],
                                                     build_context(rows), con=args.read_strategy == "con")}
                    hypo_fh.write(json.dumps(hypo, ensure_ascii=False) + "\n")
                    hypo_fh.flush()
            finally:
                store.close()
            with retr_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(res, ensure_ascii=False) + "\n")
            status = "ABS" if res["abstain"] else "ok"
            print(f"  [{qi}/{len(items)}] {qid} {status} R@k(sid)={res['recall_at_k_sid']} "
                  f"R@k(turn)={res['recall_at_k_turn']} mrr={res['mrr_sid']:.3f} hits={res['n_hits_sid']}/{res['n_retrieved']}")
            if args.sleep:
                time.sleep(args.sleep)
    finally:
        if hypo_fh:
            hypo_fh.close()

    results = list(results_by_id.values())
    results.sort(key=lambda r: r["question_id"])

    # Aggregate (abstention excluded from scores, like the official harness)
    scored = [r for r in results if not r["abstain"]]
    abstain = [r for r in results if r["abstain"]]
    n = len(scored) or 1
    summary = {
        "config": {"k": args.k, "embedder": args.embedder, "baseline": args.baseline,
                   "layer": args.layer, "granularity": args.granularity, "roles": args.roles,
                   "read_strategy": args.read_strategy, "questions": len(items)},
        "n_scored": len(scored),
        "n_abstain": len(abstain),
        "session_recall_at_k": sum(r["recall_at_k_sid"] for r in scored) / n,
        "turn_recall_at_k": sum(r["recall_at_k_turn"] for r in scored) / n,
        "session_mrr": sum(r["mrr_sid"] for r in scored) / n,
    }
    with open(out / "retrieval_results.jsonl", "w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(out / "summary.json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)
    if hypotheses:
        with open(out / "hypothesis.jsonl", "w", encoding="utf-8") as fh:
            for h in hypotheses:
                fh.write(json.dumps(h, ensure_ascii=False) + "\n")
    print("\n[summary]")
    for key, val in summary.items():
        if isinstance(val, float):
            print(f"  {key}: {val:.4f}")
        else:
            print(f"  {key}: {val}")
    print(f"[out] {out}/summary.json  retrieval_results.jsonl"
          + ("  hypothesis.jsonl" if hypotheses else "")
          + "\n      (QA accuracy: run official repo's evaluate_qa.py on hypothesis.jsonl)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
