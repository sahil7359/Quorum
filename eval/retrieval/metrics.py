"""Retrieval metrics.

Binary relevance. Definitions are written out because the difference between two plausible
definitions of Recall@k is a factor that would silently change a published number.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def ndcg_at_k(retrieved: Sequence[str], relevant: set[str], k: int) -> float:
    """Normalised discounted cumulative gain, binary gains.

    DCG uses ``1 / log2(rank + 1)`` with 1-based ranks. The ideal ranking places
    ``min(len(relevant), k)`` relevant items first -- capping at ``k`` matters, because
    normalising against *all* relevant items would make a perfect top-5 score below 1.0
    whenever more than five chunks are relevant, and the metric would look broken.
    """
    if not relevant:
        return 0.0

    dcg = sum(
        1.0 / math.log2(rank + 2) for rank, doc_id in enumerate(retrieved[:k]) if doc_id in relevant
    )
    ideal = sum(1.0 / math.log2(rank + 2) for rank in range(min(len(relevant), k)))
    return dcg / ideal if ideal else 0.0


def recall_at_k(retrieved: Sequence[str], relevant: set[str], k: int) -> float:
    """Fraction of the *reachable* relevant items found in the top k.

    Normalised by ``min(len(relevant), k)`` rather than ``len(relevant)``. With k=5 and 12
    relevant chunks, the plain definition caps at 0.42 no matter how good retrieval is,
    which measures the label set rather than the retriever. Stated here so the number is
    interpretable by someone who did not write it.
    """
    if not relevant:
        return 0.0
    hits = sum(1 for doc_id in retrieved[:k] if doc_id in relevant)
    return hits / min(len(relevant), k)


def success_at_k(retrieved: Sequence[str], relevant: set[str], k: int) -> float:
    """1.0 if any relevant chunk appears in the top k.

    The metric closest to what actually matters for Quorum: a specialist needs *one* apt
    chunk to ground a finding, not a well-ordered list.
    """
    return 1.0 if any(doc_id in relevant for doc_id in retrieved[:k]) else 0.0


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0
