"""The chunk store against real Postgres + pgvector, not just in-memory.

Needs a real Postgres reachable at ``QUORUM_TEST_DATABASE_URL`` (defaults to the local
``quorum-postgres`` Docker container on port 5433, same as ``test_postgres_audit.py``).
"""

from __future__ import annotations

import os
import random

import pytest

from app.domain.entities import Chunk, RepoRef
from app.domain.values import ChunkId, ChunkLocator
from app.infrastructure.persistence.postgres_chunk_store import PostgresChunkStore
from app.infrastructure.retrieval.dense import InMemoryChunkStore

pytestmark = pytest.mark.integration

DSN = os.environ.get("QUORUM_TEST_DATABASE_URL", "postgresql://quorum:quorum@localhost:5433/quorum")
REPO = RepoRef.parse("acme/widget")
DIMENSIONS = 384


def a_chunk(index: int, *, commit_sha: str = "sha1") -> Chunk:
    return Chunk.create(
        locator=ChunkLocator(
            repo="acme/widget",
            commit_sha=commit_sha,
            file_path=f"doc{index}.md",
            section_path=f"Section {index}",
            start_offset=0,
            end_offset=100,
        ),
        content=f"chunk number {index}",
        start_line=1,
        end_line=1,
        ordinal=0,
        token_count=10,
    )


def a_vector(seed: int) -> list[float]:
    rng = random.Random(seed)
    return [rng.uniform(-1, 1) for _ in range(DIMENSIONS)]


async def a_store() -> PostgresChunkStore:
    store = await PostgresChunkStore.connect(DSN)
    # This suite reuses one real database across runs; each test's chunk ids include a run
    # marker so leftovers from a previous run cannot masquerade as this run's data.
    return store


class TestBasicOperations:
    async def test_upsert_then_get_round_trips(self) -> None:
        store = await a_store()
        chunk = a_chunk(1, commit_sha="basic-get")
        await store.upsert([chunk], [a_vector(1)])

        got = await store.get(chunk.chunk_id)
        assert got is not None
        assert got.content == chunk.content
        assert got.locator == chunk.locator
        await store.close()

    async def test_get_of_an_absent_chunk_is_none(self) -> None:
        store = await a_store()
        assert await store.get(ChunkId("0000000000000000")) is None
        await store.close()

    async def test_search_dense_is_scoped_to_repo_and_commit(self) -> None:
        store = await a_store()
        chunk_a = a_chunk(1, commit_sha="scope-a")
        chunk_b = a_chunk(1, commit_sha="scope-b")
        await store.upsert([chunk_a], [a_vector(10)])
        await store.upsert([chunk_b], [a_vector(11)])

        results = await store.search_dense(a_vector(10), repo=REPO, commit_sha="scope-a", limit=5)
        assert len(results) == 1
        assert results[0].chunk.locator.commit_sha == "scope-a"
        await store.close()

    async def test_all_for_repo_returns_every_chunk_at_that_commit(self) -> None:
        store = await a_store()
        commit = "all-for-repo"
        chunks = [a_chunk(i, commit_sha=commit) for i in range(3)]
        await store.upsert(chunks, [a_vector(i) for i in range(3)])

        found = await store.all_for_repo(REPO, commit)
        assert {c.chunk_id for c in found} == {c.chunk_id for c in chunks}
        await store.close()

    async def test_upsert_rejects_mismatched_lengths(self) -> None:
        store = await a_store()
        with pytest.raises(ValueError, match="misalignment"):
            await store.upsert([a_chunk(1)], [])
        await store.close()


class TestAgreementWithInMemory:
    async def test_postgres_and_in_memory_agree_on_top_k_for_a_fixed_query_set(self) -> None:
        """HANDOFF.md's named risk: pgvector's HNSW index is *approximate* nearest-neighbour
        search; InMemoryChunkStore (what the committed retrieval baseline was measured
        against) is an exhaustive, exact scan. They can disagree near a score tie. This does
        not prove they always agree -- it proves that on a small, fixed, non-adversarial
        corpus (the case this project's actual six-repo scale looks like) they do, which is
        the deployment-relevant claim HANDOFF asked this test to make."""
        commit = "agreement-check"
        chunks = [a_chunk(i, commit_sha=commit) for i in range(20)]
        vectors = [a_vector(i) for i in range(20)]

        pg_store = await a_store()
        await pg_store.upsert(chunks, vectors)

        mem_store = InMemoryChunkStore()
        await mem_store.upsert(chunks, vectors)

        for query_seed in (100, 101, 102, 103, 104):
            query = a_vector(query_seed)
            pg_results = await pg_store.search_dense(query, repo=REPO, commit_sha=commit, limit=5)
            mem_results = await mem_store.search_dense(query, repo=REPO, commit_sha=commit, limit=5)

            pg_ids = [r.chunk.chunk_id for r in pg_results]
            mem_ids = [r.chunk.chunk_id for r in mem_results]
            assert pg_ids == mem_ids, (
                f"top-5 disagreement for query seed {query_seed}: pg={pg_ids} mem={mem_ids}"
            )

        await pg_store.close()
