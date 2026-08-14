"""Reciprocal Rank Fusion.

Dense cosine similarity lives in roughly [0, 1]; BM25 scores are unbounded and depend on
corpus statistics. Any attempt to combine them by *value* needs normalisation, and every
normalisation scheme I know of (min-max, z-score) is unstable when one leg returns few
results or when scores cluster.

RRF sidesteps the problem by ignoring scores entirely and combining **ranks**:

    score(d) = sum over rankers of  1 / (k + rank(d))

with ``k = 60``, the value from the original Cormack et al. paper. It is the boring,
robust choice and it needs no tuning, which is exactly what I want for a component whose
quality I am about to measure — if the fusion had three knobs, a good NDCG would tell me
more about my tuning than about hybrid retrieval.
"""

from __future__ import annotations

from collections.abc import Sequence

RRF_K = 60


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[str]], *, k: int = RRF_K, limit: int | None = None
) -> list[tuple[str, float]]:
    """Fuse several ranked id lists into one.

    Args:
        rankings: ranked lists, best first. Lists may differ in length and overlap partially.
        k: the RRF constant. Larger flattens the contribution of top ranks.
        limit: truncate the fused result.
    """
    scores: dict[str, float] = {}
    first_seen: dict[str, int] = {}

    for ranking in rankings:
        for rank, doc_id in enumerate(ranking):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
            first_seen.setdefault(doc_id, rank)

    # Ties broken by best rank achieved in any single list, then by id, so fusion is
    # deterministic -- a retrieval eval that reorders between runs is not a measurement.
    ordered = sorted(scores.items(), key=lambda pair: (-pair[1], first_seen[pair[0]], pair[0]))
    return ordered[:limit] if limit is not None else ordered
