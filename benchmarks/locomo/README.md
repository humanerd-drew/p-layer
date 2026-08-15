# LoCoMo benchmark for p-layer

Runs [LoCoMo](https://github.com/snap-research/locomo) (Snap Research,
ACL 2024) against p-layer's shipped retrieval path, scored with the
benchmark's own evidence labels ("D<session>:<turn>") — no LLM needed for
the retrieval lane.

## Results (2026-08-15, `locomo10.json`, 10 conversations, 1,986 QA)

| config | evidence recall@10 | MRR | n scored (adv excluded) |
|---|---|---|---|
| default (hash, FTS-only), all 10 convs | **0.5429** | 0.3442 | 1540 |
| default, convs 1-2 | 0.5322 | 0.3171 | 233 |
| bge-m3 hybrid, convs 1-2 | 0.4120 | 0.1463 | 233 |

By category (default, all 10): single-hop 0.369 · multi-hop 0.611 ·
temporal 0.302 · commonsense/world 0.603 · adversarial excluded (446).

## What this benchmark reveals (honest read)

**LoCoMo evidence turns are adversarially indirect — raw-turn retrieval is
structurally limited here, regardless of retriever.**

- **0/282 single-hop questions share even one content token with their
  evidence turn.** The evidence for "What did Caroline research?" is
  "Researching adoption agencies — it's been a dream to have a family…"
  (zero lexical overlap). FTS-only cannot answer this class by design.
- **A real semantic embedder (bge-m3) does not fix it and even hurts**
  (0.41 vs 0.53 on the same conversations): LoCoMo is full of generic
  conversational echo ("Wow, Caroline! What…?") that semantic similarity
  ranks above terse, indirect evidence turns. This is the opposite of the
  LongMemEval result, where bge-m3 ≥ hash — a useful tempering data point
  for the "semantic search is always better" assumption.
- **The official LoCoMo RAG pipeline indexes LLM-generated observations and
  session summaries, not raw turns.** p-layer has the same machinery
  (`consolidate` → P5 knowledge, `compile-wiki`) but applying it here is an
  LLM-heavy follow-up (and the QA lane needs a key that has the required
  model — see the model policy in the LongMemEval adapter).

## Reproduce

```bash
mkdir -p data && cd data
curl -L -o locomo10.json \
  https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json
cd ..
python3 benchmarks/locomo/run_locomo.py \
    --data benchmarks/locomo/data/locomo10.json --k 10 --embedder hash

# QA lane (model must be explicit — never substituted; judge with the
# official LoCoMo evaluator afterwards)
OPENAI_API_KEY=... python3 benchmarks/locomo/run_locomo.py --generate --model <explicit>
```

Outputs: `out/summary.json` (evidence recall@k / MRR, by category),
`out/evidence_results.jsonl` (per-question), `out/hypothesis.jsonl` (QA lane).

## Notes

- Both speakers are conversation partners; every turn is stored verbatim
  (layer P2, writer system), one fresh store per conversation.
- Adversarial questions (category 5, 446) are scored but excluded from the
  aggregates — the correct behavior there is refusal, not retrieval.
- Category-4 (commonsense/world knowledge) questions often cannot be answered
  from memory at all; treat its score as informational.
