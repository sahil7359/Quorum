"""Ingest a repo's own documentation into the chunk store, on demand.

A gap earlier phases deliberately left open: nothing before this decided *when* an arbitrary
repo's docs get indexed -- Phase 12's demo script pre-ingests a curated, hand-picked doc set
for two specific repos; the deployed service had no equivalent for a repo a real caller asks
about.

Answered the simplest way that's still correct: check-then-ingest, on the first review that
ever asks about a given ``(repo, commit_sha)``. No background job, no admin endpoint, no
"has this repo been ingested" table of its own -- the chunk store itself already answers that
question (``all_for_repo`` empty means "not yet"), and a review already has to reach the code
host to fetch the diff, so paying the one-time ingestion cost inline with the request that
needs it is simpler than building and operating a separate trigger. The real cost this defers
is latency on exactly one review per (repo, commit): the first.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain import log_events
from app.domain.entities import RepoRef
from app.domain.errors import CodeHostError
from app.domain.ports import ChunkStorePort, DocIngestionPort, EmbedderPort, LoggerPort
from app.infrastructure.retrieval.chunker import chunk_markdown


@dataclass
class IngestionService:
    doc_source: DocIngestionPort
    embedder: EmbedderPort
    store: ChunkStorePort
    logger: LoggerPort
    max_files: int = 60

    async def ensure_ingested(self, repo: RepoRef, commit_sha: str) -> int:
        """Populates the chunk store for ``(repo, commit_sha)`` if it is empty.

        Returns the number of chunks ingested -- ``0`` both when the store was already
        populated (a no-op, not a failure) and when nothing could be ingested at all (also
        not a failure: the review still runs, just with no retrieved context to cite).
        """
        existing = await self.store.all_for_repo(repo, commit_sha)
        if existing:
            self.logger.debug(
                log_events.INGESTION_SKIPPED,
                repo=str(repo),
                commit_sha=commit_sha,
                existing_chunks=len(existing),
            )
            return 0

        paths = await self.doc_source.list_markdown_files(repo, limit=self.max_files)

        chunks = []
        for path in paths:
            try:
                text = await self.doc_source.get_file(repo, path, ref=commit_sha)
            except CodeHostError as exc:
                # why caught per-file rather than left to fail the whole ingestion:
                # list_markdown_files searches the repo's default branch, not this exact
                # commit (see its docstring) -- a path existing on the branch but not at this
                # specific commit is expected, not a bug, and one missing file should not cost
                # every other file's context.
                self.logger.warn(
                    log_events.INGESTION_FILE_SKIPPED,
                    repo=str(repo),
                    path=path,
                    error=f"{type(exc).__name__}: {exc}"[:200],
                )
                continue
            chunks.extend(
                chunk_markdown(text, repo=str(repo), commit_sha=commit_sha, file_path=path)
            )

        if not chunks:
            self.logger.warn(log_events.INGESTION_EMPTY, repo=str(repo), commit_sha=commit_sha)
            return 0

        embeddings = await self.embedder.embed([c.content for c in chunks])
        count = await self.store.upsert(chunks, embeddings)
        self.logger.info(
            log_events.INGESTION_COMPLETED,
            repo=str(repo),
            commit_sha=commit_sha,
            files=len(paths),
            chunks=count,
        )
        return count
