"""Quorum's HTTP surface. ``docs/AppFlow.md``, the web door.

Deliberately thin. Everything about *how* a review is produced is ``ReviewService``'s job;
this module's job is HTTP verbs, status codes, and turning ``review_stream``'s
``(event_name, payload)`` pairs into wire-format SSE.

**Idempotency-Key and streaming are mutually exclusive by design, not by omission.** A caller
that sends the header wants "run this once, however many times I ask" -- a guarantee built on
:meth:`ReviewService.review`'s in-flight coalescing. A caller that omits it wants to watch the
review form. Coalescing an async generator's *yields* across two callers would need a
broadcast/pub-sub layer this pass does not build (see ``review_stream``'s own docstring for
why); routing the two cases to the two methods that already exist -- one that coalesces and
returns once, one that streams and does not -- is the honest boundary rather than a compromise
that half-supports both.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.domain.entities import RepoRef
from app.interface.review_service import ReviewService, review_event


class ReviewRequest(BaseModel):
    repo: str = Field(..., examples=["psf/requests"], description="owner/name")
    pr_number: int = Field(..., gt=0)


def create_app(
    service: ReviewService,
    *,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    """The composition root wires a real ``ReviewService`` and passes it here. Tests pass one
    built entirely from fakes -- this function never constructs an adapter itself.

    ``lifespan`` is optional because every test-built ``service`` is already fully usable at
    construction time (fakes need no connection step). ``code_host`` (a real
    ``GitHubMcpClient``) does: its subprocess and MCP session have to be opened inside the
    same event loop that serves requests, not at import time in a throwaway loop of their
    own, or the transport ends up bound to a loop that's already closed by the time a request
    needs it. The real composition root (``app/interface/composition.py``) is the one caller
    that passes this.
    """
    app = FastAPI(title="Quorum", version="0.1.0", lifespan=lifespan)
    # why wide open, not an allowlisted origin: this API has no cookies, no session, no
    # credential of any kind attached to a request -- every response is either public GitHub
    # PR data or a review this service already agreed to produce for anyone under the rate
    # limit. `allow_origins=["*"]` combined with credentialed requests would be a real hole;
    # combined with `allow_credentials` left at its default `False`, there is nothing here
    # for a third-party origin to steal by reading a response it wasn't supposed to see.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Idempotency-Key"],
    )

    @app.get("/")
    async def root() -> dict[str, Any]:
        """A friendly landing for anyone who opens the API's base URL directly.

        why this exists: the base URL is what people paste into a browser first, and a bare
        404 there reads as "nothing is deployed" even when the service is perfectly healthy.
        This says what the service is and points at the human UI and the docs.
        """
        return {
            "service": "Quorum",
            "what": "A supervisor agent that reviews pull requests with citation-backed findings.",
            "endpoints": {
                "health": "/healthz",
                "readiness": "/readyz",
                "status": "/api/status",
                "recent_reviews": "/api/reviews",
                "start_review": "POST /api/reviews {repo, pr_number}",
                "api_docs": "/docs",
            },
            "source": "https://github.com/sahil7359/Quorum",
        }

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness: the process can answer HTTP at all.

        No dependency is checked on purpose. A process that is healthy but not yet ready
        (mid-startup, waiting on a slow import) must still report alive here, or an
        orchestrator that conflates the two restarts a merely-slow process in a loop instead
        of waiting for ``/readyz``.
        """
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        """Readiness: the storage this process depends on is actually reachable.

        Checks the budget store only -- cheap, local, and representative of "can this process
        do its job" without also depending on GitHub or the LLM provider being up. Those are
        per-request failures (``ingest`` fails the run, ERROR, no partial review -- see
        ``docs/AppFlow.md``'s failure-paths table) and a 503 here would not usefully protect
        against them; it would just make the whole service look down over one PR's bad luck.
        """
        try:
            await service.budget.state()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"budget store unreachable: {exc}") from exc
        return {"status": "ready"}

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        """A snapshot for the dashboard's status panel: what this instance is, and its budget.

        Degrades rather than 500s if the budget store is briefly unreachable -- a status panel
        that can't render because one number is missing is worse than one showing the number as
        unknown.
        """
        budget: dict[str, Any]
        try:
            state = await service.budget.state()
            budget = {
                "consumed": state.consumed,
                "limit": state.limit,
                "exhausted": state.exhausted,
            }
        except Exception:
            budget = {"consumed": None, "limit": None, "exhausted": None}
        return {
            "provider": service.provider,
            "model": service.model_label,
            "config_hash": service.config_hash,
            "budget": budget,
        }

    @app.get("/api/reviews")
    async def list_reviews(limit: int = 20) -> list[dict[str, Any]]:
        """Recent reviews, newest first -- the dashboard's history. Bounded so a caller cannot
        ask for the whole table."""
        capped = max(1, min(limit, 100))
        reviews = await service.cache.list_recent(capped)
        return [review_event(r) for r in reviews]

    @app.post("/api/reviews")
    async def post_review(
        body: ReviewRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> EventSourceResponse:
        try:
            repo = RepoRef.parse(body.repo)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid repo: {exc}") from exc

        return EventSourceResponse(_events(service, repo, body.pr_number, idempotency_key))

    return app


async def _events(
    service: ReviewService, repo: RepoRef, pr_number: int, idempotency_key: str | None
) -> AsyncIterator[dict[str, Any]]:
    if idempotency_key is not None:
        review = await service.review(repo, pr_number, idempotency_key=idempotency_key)
        yield {"event": "review.completed", "data": json.dumps(review_event(review))}
        return

    async for name, payload in service.review_stream(repo, pr_number):
        yield {"event": name, "data": json.dumps(payload)}
