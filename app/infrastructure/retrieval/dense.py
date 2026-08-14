"""Dense embedding and an in-memory vector store.

``fastembed`` runs BAAI/bge-small-en-v1.5 as ONNX on CPU: 384 dimensions, ~130MB of model.
Chosen over ``sentence-transformers`` because Render's free tier is 512MB of RAM and torch
alone is roughly 2GB. The same model runs locally and in production, so vectors written at
ingest are comparable to vectors computed at query time -- if those diverged, retrieval
would silently return nonsense with no error anywhere.

``InMemoryChunkStore`` is the only store implemented in this phase. pgvector is deferred:
see the Phase 3 notes in HANDOFF.md for why, and what it costs.
"""

from __future__ import annotations

import hashlib
import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.domain.entities import Chunk, RepoRef, ScoredChunk
from app.domain.values import ChunkId

if TYPE_CHECKING:  # pragma: no cover - import cost only paid at runtime
    from fastembed import TextEmbedding


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


class FastEmbedEmbedder:
    """Adapter satisfying ``EmbedderPort``.

    The model is loaded lazily on first use. Constructing an embedder must not cost 10
    seconds and 130MB of download, because the composition root builds one whether or not
    the request turns out to need retrieval.
    """

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5", dimensions: int = 384) -> None:
        self._model_name = model_name
        self._dimensions = dimensions
        self._model: TextEmbedding | None = None

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def _loaded(self) -> TextEmbedding:
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(self._model_name)
        return self._model

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        if not texts:
            return []
        # fastembed is synchronous CPU work. It is called from async code because the port is
        # async (Groq and pgvector genuinely are), not because it awaits anything.
        return [list(map(float, vector)) for vector in self._loaded().embed(list(texts))]


class HashEmbedder:
    """Deterministic pseudo-embedder for unit tests.

    Hashes tokens into a fixed-width vector. It has no semantics whatsoever -- paraphrase
    scores near zero -- so it is used only where the *plumbing* is under test. Anything
    measuring retrieval quality uses the real model, because a number produced by this would
    be meaningless and worse than no number.
    """

    def __init__(self, dimensions: int = 64) -> None:
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self._dimensions
            for token in text.lower().split():
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                bucket = struct.unpack("<I", digest[:4])[0] % self._dimensions
                vector[bucket] += 1.0
            vectors.append(vector)
        return vectors


@dataclass
class InMemoryChunkStore:
    """Adapter satisfying ``ChunkStorePort``. Exhaustive scan, no index.

    Six repositories is order 1e4 chunks; a full cosine scan is single-digit milliseconds.
    An HNSW index here would be optimising a cost that does not exist while adding a
    correctness surface (recall loss from approximate search) that would contaminate the
    retrieval numbers this phase exists to produce.
    """

    chunks: dict[ChunkId, Chunk] = field(default_factory=dict)
    vectors: dict[ChunkId, list[float]] = field(default_factory=dict)

    async def upsert(self, chunks: Sequence[Chunk], embeddings: Sequence[Sequence[float]]) -> int:
        if len(chunks) != len(embeddings):
            raise ValueError(
                f"{len(chunks)} chunks but {len(embeddings)} embeddings -- a misalignment "
                "here would attach every citation to the wrong text"
            )
        for chunk, embedding in zip(chunks, embeddings, strict=True):
            self.chunks[chunk.chunk_id] = chunk
            self.vectors[chunk.chunk_id] = list(embedding)
        return len(chunks)

    async def get(self, chunk_id: ChunkId) -> Chunk | None:
        return self.chunks.get(chunk_id)

    async def search_dense(
        self,
        embedding: Sequence[float],
        *,
        repo: RepoRef,
        commit_sha: str,
        limit: int,
    ) -> Sequence[ScoredChunk]:
        scored: list[ScoredChunk] = []
        for chunk_id, vector in self.vectors.items():
            chunk = self.chunks[chunk_id]
            if chunk.locator.repo != str(repo) or chunk.locator.commit_sha != commit_sha:
                continue
            scored.append(ScoredChunk(chunk=chunk, score=cosine(embedding, vector)))

        scored.sort(key=lambda s: (-s.score, str(s.chunk_id)))
        for rank, item in enumerate(scored[:limit]):
            object.__setattr__(item, "dense_rank", rank)
        return scored[:limit]

    async def all_for_repo(self, repo: RepoRef, commit_sha: str) -> Sequence[Chunk]:
        return [
            chunk
            for chunk in self.chunks.values()
            if chunk.locator.repo == str(repo) and chunk.locator.commit_sha == commit_sha
        ]
