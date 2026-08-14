"""BM25 with a code-aware tokenizer.

Hand-rolled rather than taken from ``rank-bm25``, and the tokenizer is the reason. The whole
job of the sparse leg is to catch what dense embeddings miss: **exact identifier matches**.
A query mentioning ``RetrievalPort`` or ``QUORUM_MAX_DIFF_LINES`` must reach the chunk that
literally contains that token, and a 384-dimension bi-encoder routinely fails at this while
being confidently close on paraphrase.

Off-the-shelf BM25 tokenises on whitespace and punctuation, so ``RetrievalPort`` stays one
opaque token and a query for ``retrieval port`` misses it entirely. Splitting camelCase and
snake_case — while *also keeping the original* — is the feature, and owning ~70 lines is a
smaller cost than owning a dependency I would then have to work around.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

_WORD = re.compile(r"[A-Za-z0-9_]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

K1 = 1.2
B = 0.75


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, plus the sub-tokens of every compound identifier.

    ``getUserToken`` yields ``getusertoken``, ``get``, ``user``, ``token``.
    ``MAX_DIFF_LINES`` yields ``max_diff_lines``, ``max``, ``diff``, ``lines``.

    Keeping the whole identifier *and* its parts means an exact query still scores highest
    (it matches every token), while a natural-language query can reach it at all.
    """
    tokens: list[str] = []
    for match in _WORD.finditer(text):
        word = match.group()
        tokens.append(word.lower())

        parts = [p for chunk in word.split("_") for p in _CAMEL_BOUNDARY.split(chunk) if p]
        if len(parts) > 1:
            tokens.extend(part.lower() for part in parts)
    return tokens


@dataclass
class BM25Index:
    """Okapi BM25 over an in-memory corpus.

    Built once per (repo, commit) at ingest and reused for every query in a review. The
    corpus is order 1e4 chunks for six repositories, so a scan per query is microseconds and
    an inverted index would be premature.
    """

    doc_ids: list[str] = field(default_factory=list)
    doc_terms: list[Counter[str]] = field(default_factory=list)
    doc_lengths: list[int] = field(default_factory=list)
    document_frequency: Counter[str] = field(default_factory=Counter)

    @property
    def average_length(self) -> float:
        return sum(self.doc_lengths) / len(self.doc_lengths) if self.doc_lengths else 0.0

    @classmethod
    def build(cls, documents: list[tuple[str, str]]) -> BM25Index:
        index = cls()
        for doc_id, text in documents:
            terms = Counter(tokenize(text))
            index.doc_ids.append(doc_id)
            index.doc_terms.append(terms)
            index.doc_lengths.append(sum(terms.values()))
            index.document_frequency.update(terms.keys())
        return index

    def _idf(self, term: str) -> float:
        n_docs = len(self.doc_ids)
        df = self.document_frequency.get(term, 0)
        # why: the +0.5/+0.5 smoothed form can go negative for terms present in more than
        #      half the corpus, which then *penalises* a document for containing the query
        #      term. Clamping at zero is the standard fix and matters here because our
        #      corpus is small and domain-specific, so common terms are genuinely common.
        #      alt: raw log(N/df) (never negative, but ignores the smoothing entirely)
        return max(0.0, math.log((n_docs - df + 0.5) / (df + 0.5) + 1.0))

    def search(self, query: str, *, limit: int) -> list[tuple[str, float]]:
        query_terms = tokenize(query)
        if not query_terms or not self.doc_ids:
            return []

        avg = self.average_length or 1.0
        scored: list[tuple[str, float]] = []

        for position, doc_id in enumerate(self.doc_ids):
            terms = self.doc_terms[position]
            length = self.doc_lengths[position]
            score = 0.0
            for term in query_terms:
                frequency = terms.get(term, 0)
                if not frequency:
                    continue
                numerator = frequency * (K1 + 1)
                denominator = frequency + K1 * (1 - B + B * length / avg)
                score += self._idf(term) * numerator / denominator
            if score > 0:
                scored.append((doc_id, score))

        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return scored[:limit]
