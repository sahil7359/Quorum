"""The production composition root. ``uvicorn app.interface.composition:app``.

Wires ``Settings`` to real, Postgres-backed adapters -- the same shape ``scripts/demo.py``
(Phase 12) proved works against a real ``GitHubMcpClient``, real Ollama, and real retrieval,
but long-running behind HTTP instead of printing a gallery result and exiting.

**The chunk store starts empty.** There is no per-request "ingest this repo's docs" step yet
-- picking how to trigger ingestion (on first request? a background job? an explicit admin
endpoint?) and how to keep it fresh as a repo's default branch moves is real, undecided design
work, deliberately deferred (HANDOFF.md's R7). A review against a repo nobody has ingested
degrades to zero retrieved context per specialist rather than erroring -- ``HybridRetriever``
and its chunk store both treat an empty result set as "nothing matched", not a failure -- so
the service stays up, it just cites nothing for an un-ingested repo. Documented here rather
than silently discovered by whoever deploys this first.

**Every adapter needing a real connection (the MCP client, the four Postgres adapters) is
constructed twice: a throwaway, fully-valid placeholder at module level, replaced with the
real thing inside the app's lifespan.** Not a hack -- the alternative is opening those
connections eagerly at import time in a throwaway event loop of their own, then using them
later from uvicorn's actual serving loop. The GitHub MCP client's session is backed by an
``anyio`` task group tied to whichever loop opened it; a connection opened outside uvicorn's
loop would hand every request a transport bound to an already-closed one. The placeholders
are real, fully-initialised objects (in-memory / SQLite, the same ones the demo script and
Phase 8's tests already use) rather than half-constructed stand-ins, so nothing here is ever
in a broken state even if something touched it before startup finished -- it would just be
talking to an empty in-memory store, not crashing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.domain.ports import ChatModelPort
from app.infrastructure.clock import SystemClock
from app.infrastructure.config import Settings
from app.infrastructure.llm.groq import GroqChatModel
from app.infrastructure.llm.ollama import OllamaChatModel
from app.infrastructure.mcp.github_client import GitHubMcpClient
from app.infrastructure.observability.logging import StructlogLogger, configure_structlog
from app.infrastructure.observability.tracing import StructlogTracer
from app.infrastructure.persistence.budget import SqliteBudgetTracker
from app.infrastructure.persistence.postgres_budget import PostgresBudgetTracker
from app.infrastructure.persistence.postgres_chunk_store import PostgresChunkStore
from app.infrastructure.persistence.postgres_rate_limiter import PostgresRateLimiter
from app.infrastructure.persistence.postgres_review_cache import PostgresReviewCache
from app.infrastructure.persistence.rate_limiter import SqliteRateLimiter
from app.infrastructure.persistence.review_cache import SqliteReviewCache
from app.infrastructure.retrieval.dense import FastEmbedEmbedder, InMemoryChunkStore
from app.infrastructure.retrieval.hybrid import HybridRetriever
from app.interface.api.app import create_app
from app.interface.review_service import ReviewService

settings = Settings()
configure_structlog(log_level=settings.log_level, log_format=settings.log_format)
logger = StructlogLogger()
tracer = StructlogTracer(logger)
clock = SystemClock()

model: ChatModelPort
if settings.llm_provider == "groq":
    model = GroqChatModel(
        api_key=settings.groq_api_key,
        base_url=settings.groq_base_url,
        model=settings.groq_specialist_model,
        logger=logger,
    )
else:
    model = OllamaChatModel(
        base_url=settings.ollama_base_url,
        model=settings.ollama_specialist_model,
        logger=logger,
    )

embedder = FastEmbedEmbedder(model_name=settings.embedding_model)

client = GitHubMcpClient(
    command=settings.github_mcp_command,
    args=settings.github_mcp_argv,
    token=settings.github_token,
    logger=logger,
)

service = ReviewService(
    code_host=client,
    route_model=model,
    specialist_model=model,
    retriever=HybridRetriever(
        store=InMemoryChunkStore(),
        embedder=embedder,
        logger=logger,
        candidates=settings.retrieval_candidates,
        rerank_enabled=settings.rerank_enabled,
    ),
    cache=SqliteReviewCache(),
    budget=SqliteBudgetTracker(limit=settings.daily_token_budget, clock=clock),
    rate_limiter=SqliteRateLimiter(limit=settings.live_reviews_per_day, clock=clock),
    clock=clock,
    logger=logger,
    tracer=tracer,
    config_hash=f"{settings.prompt_version}:{settings.chunker_version}",
    max_diff_lines=settings.max_diff_lines,
    retrieval_top_k=settings.retrieval_top_k,
)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    async with client:
        chunk_store = await PostgresChunkStore.connect(settings.database_url)
        service.retriever = HybridRetriever(
            store=chunk_store,
            embedder=embedder,
            logger=logger,
            candidates=settings.retrieval_candidates,
            rerank_enabled=settings.rerank_enabled,
        )
        service.cache = await PostgresReviewCache.connect(settings.database_url)
        service.budget = await PostgresBudgetTracker.connect(
            settings.database_url, limit=settings.daily_token_budget, clock=clock
        )
        service.rate_limiter = await PostgresRateLimiter.connect(
            settings.database_url, limit=settings.live_reviews_per_day, clock=clock
        )
        yield


app = create_app(service, lifespan=lifespan)
