"""Hybrid retrieval: dense + BM25, fused by RRF, optionally reranked.

```
query ──┬─► dense (bge-small, cosine)      ─► N candidates ─┐
        │                                                   ├─► RRF ─► N ─► [rerank] ─► top_k
        └─► BM25 (code-aware tokenizer)    ─► N candidates ─┘
```

Reranking was on probation and **lost**. Measured on the Phase 3 eval: NDCG@5 -0.0925,
Recall@5 -0.0958 against plain hybrid, at 780ms/query versus 8.6ms. It is therefore off by
default, and kept behind a flag rather than deleted so the comparison stays reproducible.
See docs/adr/0004-rerank-disabled.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from app.domain.entities import Chunk, RepoRef, ScoredChunk
from app.domain.ports import EmbedderPort, LoggerPort
from app.domain.values import ChunkId
from app.infrastructure.retrieval.fusion import reciprocal_rank_fusion
from app.infrastructure.retrieval.sparse import BM25Index

if TYPE_CHECKING:  # pragma: no cover
    from fastembed.rerank.cross_encoder import TextCrossEncoder


class FastEmbedReranker:
    """Cross-encoder reranking. Adapter satisfying ``RerankerPort``.

    A cross-encoder reads (query, chunk) together rather than embedding them separately, so
    it can express "this passage answers this question" instead of "these are about similar
    topics". That is worth real accuracy -- if it is worth the latency, which is what the
    eval decides.
    """

    def __init__(self, model_name: str = "Xenova/ms-marco-MiniLM-L-6-v2") -> None:
        self._model_name = model_name
        self._model: TextCrossEncoder | None = None

    def _loaded(self) -> TextCrossEncoder:
        if self._model is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            self._model = TextCrossEncoder(self._model_name)
        return self._model

    async def rerank(
        self, query: str, chunks: Sequence[ScoredChunk], *, top_k: int
    ) -> Sequence[ScoredChunk]:
        if not chunks:
            return []
        scores = list(self._loaded().rerank(query, [c.chunk.content for c in chunks]))
        rescored = [
            ScoredChunk(
                chunk=candidate.chunk,
                score=float(score),
                dense_rank=candidate.dense_rank,
                sparse_rank=candidate.sparse_rank,
                rerank_score=float(score),
            )
            for candidate, score in zip(chunks, scores, strict=True)
        ]
        rescored.sort(key=lambda s: (-s.score, str(s.chunk_id)))
        return rescored[:top_k]


class HybridRetriever:
    """Adapter satisfying ``RetrieverPort``.

    The BM25 index is built per (repo, commit) and cached, because rebuilding it for each of
    three specialists in a single review would triple the cost of the cheapest part of the
    pipeline for no benefit.
    """

    def __init__(
        self,
        *,
        store: object,
        embedder: EmbedderPort,
        logger: LoggerPort,
        reranker: object | None = None,
        candidates: int = 30,
        rerank_enabled: bool = False,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._logger = logger
        self._reranker = reranker
        self._candidates = candidates
        self._rerank_enabled = rerank_enabled
        self._bm25_cache: dict[tuple[str, str], tuple[BM25Index, dict[str, Chunk]]] = {}

    async def _bm25_for(self, repo: RepoRef, commit_sha: str) -> tuple[BM25Index, dict[str, Chunk]]:
        key = (str(repo), commit_sha)
        if key not in self._bm25_cache:
            chunks = await self._store.all_for_repo(repo, commit_sha)  # type: ignore[attr-defined]
            by_id = {str(c.chunk_id): c for c in chunks}
            index = BM25Index.build([(str(c.chunk_id), c.content) for c in chunks])
            self._bm25_cache[key] = (index, by_id)
        return self._bm25_cache[key]

    async def retrieve(
        self,
        query: str,
        *,
        repo: RepoRef,
        commit_sha: str,
        top_k: int,
    ) -> Sequence[ScoredChunk]:
        embedding = (await self._embedder.embed([query]))[0]
        dense = await self._store.search_dense(  # type: ignore[attr-defined]
            embedding, repo=repo, commit_sha=commit_sha, limit=self._candidates
        )

        index, by_id = await self._bm25_for(repo, commit_sha)
        sparse = index.search(query, limit=self._candidates)

        dense_ids = [str(s.chunk_id) for s in dense]
        sparse_ids = [doc_id for doc_id, _ in sparse]
        fused = reciprocal_rank_fusion([dense_ids, sparse_ids], limit=self._candidates)

        dense_rank = {doc_id: rank for rank, doc_id in enumerate(dense_ids)}
        sparse_rank = {doc_id: rank for rank, doc_id in enumerate(sparse_ids)}
        by_id.update({str(s.chunk_id): s.chunk for s in dense})

        candidates = [
            ScoredChunk(
                chunk=by_id[doc_id],
                score=score,
                dense_rank=dense_rank.get(doc_id),
                sparse_rank=sparse_rank.get(doc_id),
            )
            for doc_id, score in fused
            if doc_id in by_id
        ]

        reranked = False
        if self._rerank_enabled and self._reranker is not None and candidates:
            candidates = list(await self._reranker.rerank(query, candidates, top_k=top_k))  # type: ignore[attr-defined]
            reranked = True
        else:
            candidates = candidates[:top_k]

        self._logger.info(
            "retrieval.completed",
            query_chars=len(query),
            dense_hits=len(dense_ids),
            sparse_hits=len(sparse_ids),
            fused=len(fused),
            reranked=reranked,
            survivors=[str(c.chunk_id) for c in candidates],
        )
        return candidates


def visible_ids(results: Sequence[ScoredChunk]) -> list[ChunkId]:
    """The ids a specialist was actually shown -- the input to cite-or-drop's fourth check."""
    return [result.chunk_id for result in results]
