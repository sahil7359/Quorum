"""Real composition root: wires ``Settings`` to real adapters and runs an actual review.

Nobody had written this yet -- ``app/interface/api/app.py``'s ``create_app(service)`` takes an
already-built ``ReviewService`` and every adapter constructor has only ever been exercised from
a test. This is the first place a real ``GitHubMcpClient`` (talking to the actual
``ghcr.io/github/github-mcp-server`` container), a real ``OllamaChatModel``, and a real
``FastEmbedEmbedder`` get wired together and pointed at a genuine merged pull request.

Read-only. No approval flow, no ``post_review_comment`` -- posting a real comment on someone
else's repository needs a GitHub App and a deliberately chosen throwaway PR, neither of which
exist yet (see HANDOFF.md). This prints findings; it does not publish them.

Gallery: python/mypy and psf/black, reusing Phase 6's already-validated criteria (real docs,
real merged PRs with substantive inline review comments) and two PRs from that same golden set,
so the PR numbers here aren't a fresh guess -- they're ones already known to carry the kind of
review commentary that makes "did Quorum find something real" a meaningful question.

Docs are ingested at each PR's own ``head_sha``, not a branch name -- retrieval keys chunks by
(repo, commit_sha) and the review graph queries with the PR's real head_sha (see
``nodes.py``'s ``SpecialistsNode``), so ingesting under a branch name would silently retrieve
zero chunks for every specialist, the "green test for the wrong reason" failure mode this
project has hit more than once and now checks for deliberately.

Usage: ``uv run python -m scripts.demo``
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from app.domain.entities import RepoRef
from app.infrastructure.clock import SystemClock
from app.infrastructure.config import Settings
from app.infrastructure.llm.ollama import OllamaChatModel
from app.infrastructure.mcp.github_client import GitHubMcpClient
from app.infrastructure.observability.logging import StructlogLogger, configure_structlog
from app.infrastructure.observability.tracing import StructlogTracer
from app.infrastructure.persistence.budget import SqliteBudgetTracker
from app.infrastructure.persistence.rate_limiter import SqliteRateLimiter
from app.infrastructure.persistence.review_cache import SqliteReviewCache
from app.infrastructure.retrieval.chunker import chunk_markdown
from app.infrastructure.retrieval.dense import FastEmbedEmbedder, InMemoryChunkStore
from app.infrastructure.retrieval.hybrid import HybridRetriever
from app.interface.review_service import ReviewService, review_event


@dataclass(frozen=True)
class GalleryEntry:
    repo: str
    pr_number: int
    doc_paths: tuple[str, ...]


GALLERY: tuple[GalleryEntry, ...] = (
    GalleryEntry(
        repo="python/mypy",
        pr_number=21647,
        doc_paths=(
            "README.md",
            "CONTRIBUTING.md",
            "CHANGELOG.md",
            "docs/README.md",
            "mypyc/README.md",
        ),
    ),
    GalleryEntry(
        repo="psf/black",
        pr_number=5237,
        doc_paths=(
            "README.md",
            "CONTRIBUTING.md",
            "docs/the_black_code_style/current_style.md",
            "docs/faq.md",
            "docs/usage_and_configuration/the_basics.md",
        ),
    ),
)


async def ingest(
    client: GitHubMcpClient,
    entry: GalleryEntry,
    embedder: FastEmbedEmbedder,
    store: InMemoryChunkStore,
) -> str:
    """Fetches the gallery entry's PR metadata, then ingests its curated doc set at that
    exact ``head_sha`` -- returns the head_sha so the caller reviews the same commit."""
    repo = RepoRef.parse(entry.repo)
    pull_request = await client.get_pull_request(repo, entry.pr_number)
    head_sha = pull_request.head_sha

    chunks = []
    for path in entry.doc_paths:
        text = await client.get_file(repo, path, ref=head_sha)
        chunks.extend(
            chunk_markdown(text, repo=entry.repo, commit_sha=head_sha, file_path=path)
        )
    if not chunks:
        raise SystemExit(f"no chunks ingested for {entry.repo} -- check doc_paths still exist")

    embeddings = await embedder.embed([c.content for c in chunks])
    await store.upsert(chunks, embeddings)
    print(f"  ingested {len(chunks)} chunks from {len(entry.doc_paths)} files @ {head_sha[:8]}")
    return head_sha


async def main() -> None:
    settings = Settings()
    configure_structlog(log_level=settings.log_level, log_format="console")
    logger = StructlogLogger()
    tracer = StructlogTracer(logger)
    clock = SystemClock()

    if settings.llm_provider != "ollama":
        raise SystemExit(
            f"this demo runs against Ollama only (QUORUM_LLM_PROVIDER=ollama); "
            f"got {settings.llm_provider!r}. The Groq adapter has never been verified live "
            "-- see HANDOFF.md's credential-blocked items."
        )
    model = OllamaChatModel(
        base_url=settings.ollama_base_url,
        model=settings.ollama_specialist_model,
        logger=logger,
    )

    embedder = FastEmbedEmbedder(model_name=settings.embedding_model)
    store = InMemoryChunkStore()
    retriever = HybridRetriever(
        store=store,
        embedder=embedder,
        logger=logger,
        candidates=settings.retrieval_candidates,
        rerank_enabled=settings.rerank_enabled,
    )

    output_dir = Path(__file__).resolve().parent / "demo_output"
    output_dir.mkdir(exist_ok=True)

    async with GitHubMcpClient(
        command=settings.github_mcp_command,
        args=settings.github_mcp_argv,
        token=settings.github_token,
        logger=logger,
    ) as client:
        service = ReviewService(
            code_host=client,
            route_model=model,
            specialist_model=model,
            retriever=retriever,
            cache=SqliteReviewCache(),
            budget=SqliteBudgetTracker(limit=settings.daily_token_budget, clock=clock),
            rate_limiter=SqliteRateLimiter(limit=settings.live_reviews_per_day, clock=clock),
            clock=clock,
            logger=logger,
            tracer=tracer,
            config_hash="demo",
            max_diff_lines=settings.max_diff_lines,
            retrieval_top_k=settings.retrieval_top_k,
        )

        print(f"gallery: {len(GALLERY)} repositories\n")
        for entry in GALLERY:
            print(f"{entry.repo}#{entry.pr_number}")
            await ingest(client, entry, embedder, store)

            review = await service.review(RepoRef.parse(entry.repo), entry.pr_number)
            payload = review_event(review)
            print(f"  status: {payload['status']}")
            print(f"  findings: {len(payload['findings'])}")
            for finding in payload["findings"]:
                print(f"    [{finding['severity']}] {finding['title']}")
            if payload["error"]:
                print(f"  error: {payload['error']}")

            out_path = output_dir / f"{entry.repo.replace('/', '-')}-{entry.pr_number}.json"
            out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            print(f"  full output: {out_path.relative_to(Path.cwd())}\n")


if __name__ == "__main__":
    asyncio.run(main())
