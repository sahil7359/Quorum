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
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.domain.entities import RepoRef
from app.interface.review_service import ReviewService, review_event


class ReviewRequest(BaseModel):
    repo: str = Field(..., examples=["psf/requests"], description="owner/name")
    pr_number: int = Field(..., gt=0)


def create_app(service: ReviewService) -> FastAPI:
    """The composition root wires a real ``ReviewService`` and passes it here. Tests pass one
    built entirely from fakes -- this function never constructs an adapter itself."""
    app = FastAPI(title="Quorum", version="0.1.0")

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
