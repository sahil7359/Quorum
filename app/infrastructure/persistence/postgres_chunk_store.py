"""The chunk store, Postgres + pgvector edition.

``InMemoryChunkStore`` (Phase 3) does an exhaustive cosine scan and was a deliberate deferral,
not an omission -- see its own docstring: "pgvector is a deployment concern." This is that
adapter. The one thing worth being honest about making it real: pgvector's HNSW index is
**approximate** nearest-neighbour search. The in-memory store is exact. They can disagree on
which chunks rank in the top-k, especially near a score tie, and the committed retrieval
baseline (``eval/baselines/retrieval.json``) was measured against the exact scan. HANDOFF.md
named the test this adapter needed before treating that baseline as still valid here:
``test_postgres_and_in_memory_agree_on_top_k_for_a_fixed_query_set``, in
``tests/integration/test_postgres_chunk_store.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from typing import Any, Self

import psycopg
from pgvector.psycopg import register_vector

from app.domain.entities import Chunk, RepoRef, ScoredChunk
from app.domain.values import ChunkId, ChunkLocator
from app.infrastructure.persistence.reconnecting import ReconnectingConnection


def _register_pgvector(conn: psycopg.Connection[Any]) -> None:
    # why the extension is (idempotently) ensured here, not just in SCHEMA: this runs on every
    # (re)connect, and register_vector needs the `vector` type to already exist to look up its
    # OID. On a reconnect the extension is long since created, but ensuring it keeps the
    # callback self-contained and correct on a brand-new database too.
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    register_vector(conn)


SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id        TEXT PRIMARY KEY,
    repo            TEXT        NOT NULL,
    commit_sha      TEXT        NOT NULL,
    file_path       TEXT        NOT NULL,
    section_path    TEXT        NOT NULL,
    heading_level   SMALLINT    NOT NULL,
    start_offset    INTEGER     NOT NULL,
    end_offset      INTEGER     NOT NULL,
    start_line      INTEGER     NOT NULL,
    end_line        INTEGER     NOT NULL,
    ordinal         INTEGER     NOT NULL,
    token_count     INTEGER     NOT NULL,
    content         TEXT        NOT NULL,
    content_sha     TEXT        NOT NULL,
    embedding       VECTOR(384),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (repo, commit_sha, file_path, start_offset, end_offset)
);

CREATE INDEX IF NOT EXISTS chunks_embedding_idx ON chunks
    USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_repo_sha_idx ON chunks (repo, commit_sha);
"""


class PostgresChunkStore:
    """Adapter satisfying ``ChunkStorePort``. Sync psycopg wrapped in threads -- see
    ``postgres_audit.py``'s module docstring for why (Windows event-loop policy conflict with
    the async MCP stdio tests)."""

    def __init__(self, connection: ReconnectingConnection) -> None:
        self._connection = connection

    @classmethod
    async def connect(cls, dsn: str) -> Self:
        return await asyncio.to_thread(cls._connect_sync, dsn)

    @classmethod
    def _connect_sync(cls, dsn: str) -> Self:
        # configure=_register_pgvector runs on every (re)connect, so a reopened connection has
        # the vector type adapter registered again -- otherwise the first query after a
        # reconnect would send a Python list where a vector is expected and fail.
        connection = ReconnectingConnection(dsn, configure=_register_pgvector)
        with connection.cursor() as cursor:
            cursor.execute(SCHEMA)
        return cls(connection)

    async def upsert(self, chunks: Sequence[Chunk], embeddings: Sequence[Sequence[float]]) -> int:
        if len(chunks) != len(embeddings):
            raise ValueError(
                f"{len(chunks)} chunks but {len(embeddings)} embeddings -- a misalignment "
                "here would attach every citation to the wrong text"
            )
        return await asyncio.to_thread(self._upsert_sync, chunks, embeddings)

    def _upsert_sync(self, chunks: Sequence[Chunk], embeddings: Sequence[Sequence[float]]) -> int:
        with self._connection.cursor() as cursor:
            for chunk, embedding in zip(chunks, embeddings, strict=True):
                locator = chunk.locator
                content_sha = hashlib.sha256(chunk.content.encode("utf-8")).hexdigest()
                cursor.execute(
                    "INSERT INTO chunks (chunk_id, repo, commit_sha, file_path, section_path, "
                    "heading_level, start_offset, end_offset, start_line, end_line, ordinal, "
                    "token_count, content, content_sha, embedding) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (chunk_id) DO UPDATE SET embedding = EXCLUDED.embedding, "
                    "content = EXCLUDED.content, content_sha = EXCLUDED.content_sha",
                    (
                        str(chunk.chunk_id),
                        locator.repo,
                        locator.commit_sha,
                        locator.file_path,
                        locator.section_path,
                        chunk.heading_level,
                        locator.start_offset,
                        locator.end_offset,
                        chunk.start_line,
                        chunk.end_line,
                        chunk.ordinal,
                        chunk.token_count,
                        chunk.content,
                        content_sha,
                        list(embedding),
                    ),
                )
        return len(chunks)

    async def get(self, chunk_id: ChunkId) -> Chunk | None:
        return await asyncio.to_thread(self._get_sync, chunk_id)

    def _get_sync(self, chunk_id: ChunkId) -> Chunk | None:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT * FROM chunks WHERE chunk_id = %s", (str(chunk_id),))
            row = cursor.fetchone()
        return _to_chunk(row) if row is not None else None

    async def search_dense(
        self,
        embedding: Sequence[float],
        *,
        repo: RepoRef,
        commit_sha: str,
        limit: int,
    ) -> Sequence[ScoredChunk]:
        return await asyncio.to_thread(self._search_dense_sync, embedding, repo, commit_sha, limit)

    def _search_dense_sync(
        self, embedding: Sequence[float], repo: RepoRef, commit_sha: str, limit: int
    ) -> Sequence[ScoredChunk]:
        with self._connection.cursor() as cursor:
            # why: <=> is pgvector's cosine *distance* operator (0 = identical, 2 = opposite).
            #      score = 1 - distance converts it to the cosine *similarity* every other
            #      retriever in this codebase already scores on, so RRF fusion and the
            #      retrieval eval's metrics do not need a special case for this store.
            # why: %s::vector -- register_vector adapts a vector *column* to a Python list on
            #      the way out, but an outgoing plain list parameter defaults to a
            #      double-precision array, which <=> (pgvector's cosine-distance operator)
            #      has no overload for. The explicit cast is what tells Postgres which
            #      <=> to use.
            cursor.execute(
                "SELECT *, 1 - (embedding <=> %s::vector) AS score FROM chunks "
                "WHERE repo = %s AND commit_sha = %s "
                "ORDER BY embedding <=> %s::vector LIMIT %s",
                (list(embedding), str(repo), commit_sha, list(embedding), limit),
            )
            rows = cursor.fetchall()
        return [
            ScoredChunk(chunk=_to_chunk(row), score=float(row["score"]), dense_rank=rank)
            for rank, row in enumerate(rows)
        ]

    async def all_for_repo(self, repo: RepoRef, commit_sha: str) -> Sequence[Chunk]:
        return await asyncio.to_thread(self._all_for_repo_sync, repo, commit_sha)

    def _all_for_repo_sync(self, repo: RepoRef, commit_sha: str) -> Sequence[Chunk]:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM chunks WHERE repo = %s AND commit_sha = %s", (str(repo), commit_sha)
            )
            rows = cursor.fetchall()
        return [_to_chunk(row) for row in rows]

    async def close(self) -> None:
        await asyncio.to_thread(self._connection.close)


def _to_chunk(row: dict[str, Any]) -> Chunk:
    locator = ChunkLocator(
        repo=row["repo"],
        commit_sha=row["commit_sha"],
        file_path=row["file_path"],
        section_path=row["section_path"],
        start_offset=row["start_offset"],
        end_offset=row["end_offset"],
    )
    return Chunk(
        chunk_id=ChunkId(row["chunk_id"]),
        locator=locator,
        content=row["content"],
        start_line=row["start_line"],
        end_line=row["end_line"],
        ordinal=row["ordinal"],
        token_count=row["token_count"],
        heading_level=row["heading_level"],
    )
