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

These are the numbers in the committed baseline, `eval/baselines/retrieval.json`
(corpus sha recorded there; reproduce with `uv run python -m eval.retrieval.runner`):

| config | NDCG@5 | Recall@5 | Success@5 | ms/query |
| --- | --- | --- | --- | --- |
| dense only | 0.5768 | 0.5867 | 0.9000 | 8.91 |
| BM25 only | 0.5507 | 0.6408 | 0.8500 | 0.58 |
| **hybrid (RRF)** | **0.5825** | **0.6283** | **0.9500** | **9.55** |
| hybrid + rerank | 0.5018 | 0.5575 | 0.8500 | 803.56 |

**Rerank delta: NDCG@5 −0.0807, Recall@5 −0.0708, at 84× the latency.**

An earlier run of the same eval gave hybrid 0.5943/0.6533 and a delta of −0.0925/−0.0958.
The difference is *not* noise and not a code change: the eval corpus is this repository's
own `docs/` tree, and writing this very ADR added a document to it. That feedback loop is a
real methodological wart, recorded in `learn/03` and mitigated by the corpus fingerprint the
gate now checks. The conclusion is unaffected — reranking loses on every metric in both runs.

It lost on every quality metric *and* cost 772ms more per query. There is no trade-off to
weigh here; on this corpus it is simply worse.

Two secondary results worth recording:

- **Hybrid beats both legs individually** — the reason the sparse leg exists is confirmed.
  BM25 alone has better recall than dense alone (0.6408 vs 0.5867), which is the exact-
  identifier effect the code-aware tokenizer was built for, and fusion keeps both.
- **Success@5 of 0.95 for hybrid** is the number closest to what Quorum actually needs: a
  specialist needs *one* apt chunk to ground a finding, not a well-ordered list.

Before accepting the result I verified the reranker was not integrated backwards — a
sign-flipped score would produce exactly this shape of loss. Scored against three passages
with one obviously relevant, the model returns +6.5 for the relevant passage and −11.3/−11.4
for the irrelevant ones, and my code sorts descending. The integration is correct.

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
