"""``IngestionService``, with a fake doc source and the real in-memory chunk store + a
deterministic embedder -- ``HashEmbedder`` and ``InMemoryChunkStore`` exist specifically so
plumbing like this can be tested without a real model or a real database, per their own
docstrings.
"""

from __future__ import annotations

from app.domain.entities import RepoRef
from app.infrastructure.retrieval.dense import HashEmbedder, InMemoryChunkStore
from app.interface.ingestion_service import IngestionService
from tests.support.fakes import FakeCodeHost, RecordingLogger

REPO = RepoRef.parse("acme/widget")


def doc(marker: str) -> str:
    """A body comfortably over the chunker's 16-token minimum, carrying a marker word a test
    can search for."""
    return (
        f"# Widget\n\nThis is documentation about the widget, marker {marker}, written with "
        "enough words that the chunker's minimum-token threshold for keeping a section is "
        "comfortably exceeded by this paragraph alone.\n"
    )


def a_service(doc_source: FakeCodeHost, *, max_files: int = 60) -> IngestionService:
    return IngestionService(
        doc_source=doc_source,
        embedder=HashEmbedder(),
        store=InMemoryChunkStore(),
        logger=RecordingLogger(),
        max_files=max_files,
    )


class TestEnsureIngested:
    async def test_ingests_every_discovered_file_when_the_store_is_empty(self) -> None:
        doc_source = FakeCodeHost(
            markdown_files=["README.md", "docs/guide.md"],
            files={"README.md": doc("readme"), "docs/guide.md": doc("guide")},
        )
        service = a_service(doc_source)

        count = await service.ensure_ingested(REPO, "sha1")

        assert count > 0
        chunks = await service.store.all_for_repo(REPO, "sha1")
        assert {c.locator.file_path for c in chunks} == {"README.md", "docs/guide.md"}

    async def test_is_a_no_op_when_the_store_already_has_chunks_for_this_commit(self) -> None:
        doc_source = FakeCodeHost(markdown_files=["README.md"], files={"README.md": doc("v1")})
        service = a_service(doc_source)
        first = await service.ensure_ingested(REPO, "sha1")
        assert first > 0

        # A second call, docs "changed" underneath -- the store is keyed on commit_sha, so an
        # already-ingested commit should never be re-fetched, changed source or not.
        doc_source.files["README.md"] = doc("v2-should-not-appear")
        second = await service.ensure_ingested(REPO, "sha1")

        assert second == 0
        chunks = await service.store.all_for_repo(REPO, "sha1")
        assert all("v2-should-not-appear" not in c.content for c in chunks)

    async def test_a_different_commit_for_the_same_repo_ingests_independently(self) -> None:
        doc_source = FakeCodeHost(markdown_files=["README.md"], files={"README.md": doc("v1")})
        service = a_service(doc_source)
        await service.ensure_ingested(REPO, "sha1")

        doc_source.files["README.md"] = doc("v2")
        count = await service.ensure_ingested(REPO, "sha2")

        assert count > 0
        v1_chunks = await service.store.all_for_repo(REPO, "sha1")
        v2_chunks = await service.store.all_for_repo(REPO, "sha2")
        assert any("marker v1" in c.content for c in v1_chunks)
        assert any("marker v2" in c.content for c in v2_chunks)

    async def test_a_file_that_fails_to_fetch_is_skipped_not_fatal(self) -> None:
        """search_code indexes the default branch, not an exact commit (see
        list_markdown_files's docstring) -- a discovered path missing at this specific commit
        is expected, and must not cost every other file's context."""
        doc_source = FakeCodeHost(
            markdown_files=["README.md", "docs/gone.md"],
            files={"README.md": doc("still-here")},
            file_errors={"docs/gone.md"},
        )
        service = a_service(doc_source)

        count = await service.ensure_ingested(REPO, "sha1")

        assert count > 0
        chunks = await service.store.all_for_repo(REPO, "sha1")
        assert {c.locator.file_path for c in chunks} == {"README.md"}

    async def test_no_discoverable_markdown_files_is_not_an_error(self) -> None:
        doc_source = FakeCodeHost(markdown_files=[])
        service = a_service(doc_source)

        count = await service.ensure_ingested(REPO, "sha1")

        assert count == 0

    async def test_every_file_failing_to_fetch_is_not_an_error(self) -> None:
        doc_source = FakeCodeHost(markdown_files=["docs/gone.md"], file_errors={"docs/gone.md"})
        service = a_service(doc_source)

        count = await service.ensure_ingested(REPO, "sha1")

        assert count == 0

    async def test_max_files_caps_how_many_are_fetched(self) -> None:
        doc_source = FakeCodeHost(
            markdown_files=["a.md", "b.md", "c.md"],
            files={"a.md": doc("a"), "b.md": doc("b"), "c.md": doc("c")},
        )
        service = a_service(doc_source, max_files=2)

        await service.ensure_ingested(REPO, "sha1")

        chunks = await service.store.all_for_repo(REPO, "sha1")
        assert {c.locator.file_path for c in chunks} == {"a.md", "b.md"}
