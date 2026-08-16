# LongMemEval benchmark for p-layer

Runs the [LongMemEval](https://github.com/xiaowu0162/LongMemEval) benchmark
(CMU, ICLR 2025) against p-layer's shipped retrieval path, plus the official
GPT-4o QA judge.

## Results (2026-08-15, `xiaowu0162/longmemeval-cleaned`, `longmemeval_s_cleaned.json`)

### Retrieval (500 questions; 470 scored / 30 abstained, official labels)

| config | session recall@k | turn recall@k | session MRR@k |
|---|---|---|---|
| default (hash, FTS-only fusion), k=5 | 0.9191 | 0.7234 | 0.8482 |
| default (hash, FTS-only fusion), k=10 | 0.9511 | 0.7745 | 0.8524 |
| default (hash, FTS-only fusion), k=20 | 0.9681 | 0.8213 | 0.8534 |
| BM25 (Okapi, same corpus), k=10 | 0.9553 | 0.7915 | 0.8587 |
| user+assistant ingest, k=20 | 0.9766 | 0.7936 | 0.8865 |
| session granularity, hash, k=10 | 0.9830 | — | 0.9102 |
| session granularity, **bge-m3**, k=10 | **0.9894** | — | 0.9109 |
| bge-m3 (ollama, first 40 questions, turn) | 1.0000 | 1.0000 | 0.9875 |
| hash (same 40 questions, turn) | 1.0000 | 0.9250 | 0.9625 |
| ~~hybrid fusing hash vectors~~ k=10 (pre-fix) | 0.9298 | 0.7234 | 0.4943 |

Metrics follow the official rules: session recall@k / MRR against
`answer_session_ids`, turn recall@k against user turns carrying `has_answer`,
abstention questions scored but excluded from aggregates.

### End-to-end QA accuracy (official GPT-4o judge, gpt-4o reader)

| config | Overall | Task-avg | Abs. acc |
|---|---|---|---|
| user-only, k=10, direct read | 0.344 | — | — |
| user+assistant, k=10, direct read | 0.426 | — | — |
| **user+assistant, k=20, Chain-of-Note + chronological** | **0.536** | 0.5745 | **0.9333** |

Per-task (best config): single-session-user 0.8714 · single-session-assistant
0.875 · knowledge-update 0.6154 · multi-session 0.4286 ·
temporal-reasoning 0.3233 · single-session-preference 0.3333.

Reference points from the LongMemEval paper (LongMemEval-S): GPT-4o reading
the full ~115k-token history = 0.606; GPT-4o with oracle (evidence-only)
context = 0.87. p-layer's RAG reaches 0.536 with ~20 retrieved turns and a
zero-tuned reader — no query expansion, no fact/key expansion, no reranker.

## Three findings this benchmark produced (all addressed in source/docs)

1. **A trailing `?` in a query silently zeroed recall.** Raw query terms were
   OR-joined into an FTS5 `MATCH`; a question mark (or any FTS5 syntax char,
   or a keyword like `or`/`and`) raised a syntax error which the `except`
   swallowed into a never-matching single-phrase `LIKE`. Any real agent query
   ending in `?` got zero results. Fixed in `p_layer/store.py`: terms are
   sanitized and phrase-quoted; the fallback is now a multi-term `LIKE` OR
   (also the CJK path). Regression tests added.
2. **Fusing hash "embeddings" degraded recall.** HashEmbedder's deterministic
   pseudo-random vectors are not semantic (its own docstring says so), yet
   `recall()` fused them by default — MRR 0.49 vs 0.85 at k=10 on this
   benchmark. `Embedder` now carries a `semantic` flag; `HashEmbedder` sets
   `semantic = False`, so hash mode defaults to FTS-only while real embedders
   (ollama/bge-m3) keep the hybrid path. With a real embedder the semantic
   channel helps: full 500-question bge-m3 session run reaches 0.9894
   session recall@10 (vs 0.9830 hash on the same granularity).
3. **Reading strategy is the QA bottleneck, not retrieval.** Retrieval saturates
   (session R@20 ≈ 0.98) but naive read gives 0.426; Chain-of-Note +
   chronological order + k=20 lifts QA to 0.536, confirming the paper's
   reading-stage finding. Known limitation: FTS5 has no stemming
   ("payment" ≠ "payments") — a porter-tokenizer migration is a follow-up.
   (On LoCoMo the picture inverts: hash > bge-m3 for raw-turn retrieval —
   see benchmarks/locomo/README.md. Semantic search is not universally
   better; it depends on how indirect the evidence is.)

## Reproduce

```bash
# dataset (~280 MB), pinned cleaned variant
mkdir -p data && cd data
wget https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json
wget https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_oracle.json
cd ..

# retrieval-only, no API key, offline & deterministic (~5 min for all 500)
python3 -m unittest discover -s tests
python3 benchmarks/longmemeval/run_longmemeval.py \
    --data benchmarks/longmemeval/data/longmemeval_s_cleaned.json \
    --k 10 --embedder hash --out benchmarks/longmemeval/out/k10

# QA accuracy — REFUSED on the GPT key (luna-only rule, no substitution).
# The official harness contract is gpt-4o (reader + official judge). The GPT API
# key may only call gpt-5.6-luna, so gpt-4o is not available here; the run is
# refused rather than substituting a model. Run the QA lane only with a key that
# has gpt-4o, or skip it (retrieval-only is offline and free).
#   OPENAI_API_KEY=... python3 benchmarks/longmemeval/run_longmemeval.py \
#       --data ... --generate --model gpt-4o   # -> REFUSED (gpt-4o not on GPT key)
#   OPENAI_API_KEY=... python3 benchmarks/longmemeval/run_longmemeval.py \
#       --data ... --generate                  # -> REFUSED (no --model: contract is gpt-4o)

# official judge (clone https://github.com/xiaowu0162/LongMemEval): model_zoo has
# gpt-4o / gpt-4o-mini only. Under the luna-only GPT key the judge is refused too —
# do NOT patch luna in as a substitute. Use a key that has the judge model.
python3 LongMemEval/src/evaluation/evaluate_qa.py gpt-4o \
    out/qa/hypothesis.jsonl LongMemEval/data/longmemeval_oracle.json
python3 LongMemEval/src/evaluation/print_qa_metrics.py \
    out/qa/hypothesis.jsonl.eval-results-gpt-4o LongMemEval/data/longmemeval_oracle.json
```

## Pinned configuration (reproducibility contract)

- Dataset: `xiaowu0162/longmemeval-cleaned` `longmemeval_s_cleaned.json` only
  (the original differs slightly; do not mix)
- One fresh, isolated store per question; user turns for retrieval parity
  (`--roles all` for full-session usage); layer `P2`, writer `system`
- Recall: shipped `Store.recall` / `Store.fts_search` (no reimplementation —
  see the MeMesh adapter lesson), `use_semantic=None` default
- Headline retrieval config: hash embedder, k=10, FTS-only fusion
- Headline QA config: k=20, roles=all, Chain-of-Note + chronological read
- MRR reported at the same fixed k (RRF fusion windows scale with k, so
  cross-k MRR is not comparable)
- Judge: gpt-4o / gpt-4o-mini via the official `evaluate_qa.py` (model_zoo).
  The GPT API key is luna-only — the official judge model is NOT available on it,
  so judge runs are refused rather than substituted (never patch luna in).

> Historical note: the 2026-08-15 first-run results above were produced with
> gpt-4o (reader + official judge) before the luna-only rule was enforced.
> They are kept as-is for provenance. Re-running the QA lane requires a key
> that actually has gpt-4o; the adapter refuses to substitute a different model.

## Scope notes

### Same-harness competitor comparison — Mem0 pilot + Basic Memory (both local/zero-cost where noted)

`run_mem0.py` (Mem0 OSS) and `run_basic_memory.py` (Basic Memory, fully
local — no LLM, no API cost) run the identical protocol. Same 20 questions,
session-granularity ingest, k=10:

| system | session recall@10 | MRR | cost |
|---|---|---|---|
| **p-layer** (hash, FTS-only) | **1.0000** | 0.9500 | 0 (offline) |
| p-layer (bge-m3, ollama) | 0.9500 | 0.8083 | 0 (offline) |
| Basic Memory (local FTS+vector) | 0.8000 | 0.4255 | 0 (local) |
| Mem0 OSS (deepseek, 3q pilot) | 0.0000 | — | LLM extraction (paid) |

Basic Memory loses to p-layer on both recall (+0.20) and ranking quality
(MRR 2.2x). Mem0's 0.0 is an ingestion artifact, not a retrieval verdict:
at ~19 s/session LLM extraction and ~57% of sessions dropped by malformed
JSON from the only working LLM here (deepseek; the OpenAI key is revoked),
the evidence sessions were never stored — a full 500-question run needs days
of extraction calls. Mem0's published LongMemEval numbers come from their
cloud pipeline and are not reproducible on OSS with a substitute LLM.

Deliverables: checkpointed `run_mem0.py` (needs a reliable LLM to be fair)
and `run_basic_memory.py` (works today, zero cost). A fair Mem0 same-harness
score requires their cloud — tracked as follow-up.

- The QA lane (`--generate`) requires an explicit `--model` and is refused
  otherwise: the official harness contract is gpt-4o and the model is never
  substituted. Runs are checkpoint-resume-safe for both retrieval and QA.
- flat-bm25 (pyserini) is not installed; the pure-Python BM25 baseline in
  this adapter is metric-equivalent and dependency-free. LoCoMo evidence
  retrieval and its observation/summary-indexing follow-up are in
  `benchmarks/locomo/`.
- Never present retrieval-only numbers as QA accuracy; keep the adapter
  calling `p_layer.store`, not a private copy.
