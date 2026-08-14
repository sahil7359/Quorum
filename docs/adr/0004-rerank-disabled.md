# ADR-0004 — Cross-encoder reranking is disabled by default

- **Status:** Accepted
- **Date:** 2026-08-14
- **Phase:** 3

## Context

The design (`Design.md` §3) specified hybrid retrieval — dense + BM25 fused by RRF —
followed by a cross-encoder rerank of the fused candidates. Reranking was explicitly put
"on probation": the plan required the Phase 3 eval to report NDCG@5 and Recall@5 with and
without it, and to cut it if it did not earn its latency.

Cross-encoders usually *do* help. They read (query, chunk) jointly rather than embedding
each separately, so they can express "this passage answers this question" rather than
"these are topically similar". That is a real advantage and it is why the design assumed it.

## Decision

**Reranking is off by default** (`QUORUM_RERANK_ENABLED=false`), kept behind a flag rather
than deleted.

Measured on 20 golden queries over 157 chunks from this repository's own `docs/` tree,
`BAAI/bge-small-en-v1.5` + `Xenova/ms-marco-MiniLM-L-6-v2`:

These are the numbers in the committed baseline, `eval/baselines/retrieval.json`, measured
over 20 golden queries against the frozen corpus in `eval/corpus/` (16 files, 178 chunks).
Reproduce with `uv run python -m eval.retrieval.runner`:

| config | NDCG@5 | Recall@5 | Success@5 | ms/query |
| --- | --- | --- | --- | --- |
| dense only | 0.5148 | 0.5450 | 0.9000 | 11.66 |
| BM25 only | 0.4905 | 0.5658 | 0.8500 | 0.63 |
| **hybrid (RRF)** | **0.5260** | **0.5533** | **0.8500** | **12.33** |
| hybrid + rerank | 0.4762 | 0.5200 | 0.8000 | 776.99 |

**Rerank delta: NDCG@5 −0.0498, Recall@5 −0.0333, at 63× the latency.**

### Why these numbers are lower than the ones first recorded here

An earlier revision of this ADR quoted hybrid at NDCG@5 0.5811 / Success@5 0.9500. That was
measured against a 14-file, 157-chunk corpus. Two documents (`docs/Logging.md`, `docs/MCP.md`)
were later added to `docs/`, the corpus was re-snapshotted, and **the same 20 queries scored
lower against the larger corpus** — 0.5260 NDCG@5, 0.8500 Success@5.

That is not a regression in the retriever. It is the corpus growing more distractors: more
documents means more plausible-but-wrong chunks competing for the top 5, and my queries were
written when those two documents did not exist. It is a useful reminder that **an absolute
retrieval score is a property of the corpus as much as of the retriever**, which is exactly
why the delta is the number this ADR relies on.

**The conclusion is unchanged and held across every corpus measured:** reranking lost on every
quality metric, in all four runs, at 63–91× the latency.

## Alternatives considered

**Keep reranking on because it usually helps.** Rejected — that is deciding by reputation
against a measurement I just took. If I ship a component the eval says is harmful, the eval
is decoration.

**Delete the reranker entirely.** Tempting, and it would remove ~40 lines and a model
download. Rejected because the flag is what keeps the comparison reproducible: anyone
(including me, on a different corpus) can flip it and re-run. A deleted alternative cannot
be re-measured, and "we tried it and it lost" is only credible if it can be re-tried.

**Tune it before judging** — different rerank model, more candidates, rerank only the top 10.
Genuinely possible that a larger cross-encoder would win. Rejected *for now* on scope: a
772ms/query cost has to overcome a large deficit to be worth it on a 512MB free-tier
instance, and Phase 3 is already the largest phase. Recorded as a known unexplored option
rather than a closed question.

## Consequences

**Good**

- Retrieval is 91× faster per query, which matters directly: three specialists × one query
  each = 2.3s of pure rerank latency removed from every review.
- One fewer model to download and hold in 512MB of RAM.
- The project has a published negative result, which is more informative than a positive one
  — it shows the eval is capable of changing a decision.

**Bad, and accepted**

- The result is corpus-specific and I should not over-claim it. 20 queries over 157 chunks
  of *my own* documentation is a small, self-labelled sample. What I can defend is "on this
  corpus, with these models, reranking lost"; not "cross-encoder reranking is not worth it".
- Keeping dead-by-default code costs a little clarity.
- If the gallery corpus (six third-party repositories, Phase 12) behaves differently, this
  needs re-measuring. That is a genuine open item, not a settled one.

## Invariant and test

> **Invariant:** the reranking decision is backed by a reproducible measurement, not by
> assumption, and the comparison can be re-run at any time.

`eval/retrieval/runner.py` produces the table above; `eval/baselines/retrieval.json` is the
committed baseline; `tests/unit/test_retrieval_eval_gate.py` fails when a configuration
regresses against it.
