"""Quorum's HTTP surface, over a real ASGI transport (no real socket, but a real ASGI app
handling a real HTTP request/response cycle -- not a call into the route function directly).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.domain.entities import ChangedFile, Diff, DiffHunk, PullRequest, RepoRef
from app.infrastructure.persistence.budget import SqliteBudgetTracker
from app.infrastructure.persistence.rate_limiter import SqliteRateLimiter
from app.infrastructure.persistence.review_cache import SqliteReviewCache
from app.interface.api.app import create_app
from app.interface.review_service import ReviewService
from tests.support.fakes import (
    FakeChatModel,
    FakeCodeHost,
    FakeRetriever,
    FrozenClock,
    NullTracer,
    RecordingLogger,
)

REPO = RepoRef.parse("acme/widget")


def a_code_host() -> FakeCodeHost:
    hunk = DiffHunk("a.py", 1, 1, 1, 1, "+x = 1")
    diff = Diff(files=(ChangedFile("a.py", "modified", 1, 0, (hunk,)),), raw="diff --git a/x b/x\n")
    return FakeCodeHost(
        pull_request=PullRequest(
            repo=REPO, number=42, title="t", body="", author="a", base_sha="b", head_sha="h"
        ),
        diff=diff,
        files={"a.py": "x = 1"},
    )


def make_app(
    *,
    model: FakeChatModel | None = None,
    rate_limit: int = 1000,
    budget_limit: int = 1_000_000,
    cache: SqliteReviewCache | None = None,
) -> httpx.ASGITransport:
    clock = FrozenClock()
    model = model or FakeChatModel(
        responses={"route": ['{"specialists": ["correctness"], "reason": "x"}']}
    )
    service = ReviewService(
        code_host=a_code_host(),
        route_model=model,
        specialist_model=model,
        retriever=FakeRetriever(),
        cache=cache or SqliteReviewCache(),
        budget=SqliteBudgetTracker(limit=budget_limit, clock=clock),
        rate_limiter=SqliteRateLimiter(limit=rate_limit, clock=clock),
        clock=clock,
        logger=RecordingLogger(),
        tracer=NullTracer(),
        config_hash="test-config-hash",
        max_diff_lines=1500,
        retrieval_top_k=5,
        provider="ollama",
        model_label="llama3.1:8b",
    )
    return httpx.ASGITransport(app=create_app(service))


def _parse_sse(text: str) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    event_name = None
    for line in text.splitlines():
        if line.startswith("event: "):
            event_name = line.removeprefix("event: ")
        elif line.startswith("data: ") and event_name is not None:
            events.append((event_name, json.loads(line.removeprefix("data: "))))
    return events


class TestLifespan:
    async def test_a_given_lifespan_is_wired_into_the_app(self) -> None:
        """The real composition root (app/interface/composition.py) is the one caller that
        passes a lifespan -- it's how a real GitHubMcpClient's connection gets opened inside
        uvicorn's own serving loop instead of a throwaway one at import time. Every fake-built
        service in this file passes none, matching Phase 8's original, still-correct default.
        This only checks the plumbing, not lifecycle execution: httpx's ASGITransport doesn't
        drive the ASGI lifespan protocol at all, confirmed directly before writing this rather
        than assumed, so a test asserting the callback actually ran would need a dependency
        (asgi-lifespan's LifespanManager) this project doesn't otherwise need."""

        @asynccontextmanager
        async def lifespan(_app: FastAPI) -> AsyncIterator[None]:  # pragma: no cover - unused
            yield

        service = ReviewService(
            code_host=a_code_host(),
            route_model=FakeChatModel(responses={}),
            specialist_model=FakeChatModel(responses={}),
            retriever=FakeRetriever(),
            cache=SqliteReviewCache(),
            budget=SqliteBudgetTracker(limit=1, clock=FrozenClock()),
            rate_limiter=SqliteRateLimiter(limit=1, clock=FrozenClock()),
            clock=FrozenClock(),
            logger=RecordingLogger(),
            tracer=NullTracer(),
            config_hash="test-config-hash",
            max_diff_lines=1500,
            retrieval_top_k=5,
        )
        app = create_app(service, lifespan=lifespan)
        assert app.router.lifespan_context is lifespan


class TestHealthAndReadiness:
    async def test_healthz_is_always_ok(self) -> None:
        async with httpx.AsyncClient(transport=make_app(), base_url="http://test") as client:
            response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    async def test_readyz_reports_ready_when_storage_is_reachable(self) -> None:
        async with httpx.AsyncClient(transport=make_app(), base_url="http://test") as client:
            response = await client.get("/readyz")
        assert response.status_code == 200
        assert response.json() == {"status": "ready"}

    async def test_root_describes_the_service_instead_of_404ing(self) -> None:
        """The base URL is what people paste into a browser first; a bare 404 there reads as
        'nothing deployed' even on a healthy service."""
        async with httpx.AsyncClient(transport=make_app(), base_url="http://test") as client:
            response = await client.get("/")
        assert response.status_code == 200
        body = response.json()
        assert body["service"] == "Quorum"
        assert "/api/reviews" in str(body["endpoints"])


class TestStatusAndHistory:
    async def test_status_reports_provider_and_budget(self) -> None:
        async with httpx.AsyncClient(transport=make_app(), base_url="http://test") as client:
            response = await client.get("/api/status")
        assert response.status_code == 200
        body = response.json()
        assert "provider" in body
        assert body["budget"]["limit"] is not None

    async def test_recent_reviews_is_empty_then_lists_a_completed_review(self) -> None:
        cache = SqliteReviewCache()
        transport = make_app(cache=cache)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            empty = await client.get("/api/reviews")
            assert empty.status_code == 200
            assert empty.json() == []

            # Run a real review through the same service so it lands in the cache.
            async with client.stream(
                "POST", "/api/reviews", json={"repo": "acme/widget", "pr_number": 42}
            ) as response:
                await response.aread()

            listed = await client.get("/api/reviews")
        assert listed.status_code == 200
        rows = listed.json()
        assert len(rows) == 1
        assert rows[0]["repo"] == "acme/widget"
        assert rows[0]["pr_number"] == 42

    async def test_recent_reviews_caps_the_limit(self) -> None:
        async with httpx.AsyncClient(transport=make_app(), base_url="http://test") as client:
            response = await client.get("/api/reviews", params={"limit": 9999})
        assert response.status_code == 200


class TestPostReview:
    async def test_streams_one_event_per_node_then_completes(self) -> None:
        async with (
            httpx.AsyncClient(transport=make_app(), base_url="http://test") as client,
            client.stream(
                "POST", "/api/reviews", json={"repo": "acme/widget", "pr_number": 42}
            ) as response,
        ):
            assert response.status_code == 200
            body = await response.aread()

        events = _parse_sse(body.decode())
        names = [name for name, _ in events]
        assert names == [
            "node.ingest",
            "node.route",
            "node.specialists",
            "node.synthesise",
            "review.completed",
        ]
        final = events[-1][1]
        assert final["status"] == "proposed"
        assert final["posted_to_github"] is False

    async def test_an_invalid_repo_string_is_a_400(self) -> None:
        async with httpx.AsyncClient(transport=make_app(), base_url="http://test") as client:
            response = await client.post(
                "/api/reviews", json={"repo": "not-a-repo", "pr_number": 1}
            )
        assert response.status_code == 400

    async def test_a_non_positive_pr_number_is_a_422(self) -> None:
        """Pydantic's own validation (Field(gt=0)) -- proves the constraint is wired, not just declared."""
        async with httpx.AsyncClient(transport=make_app(), base_url="http://test") as client:
            response = await client.post(
                "/api/reviews", json={"repo": "acme/widget", "pr_number": 0}
            )
        assert response.status_code == 422

    async def test_idempotency_key_collapses_to_a_single_completed_event(self) -> None:
        async with (
            httpx.AsyncClient(transport=make_app(), base_url="http://test") as client,
            client.stream(
                "POST",
                "/api/reviews",
                json={"repo": "acme/widget", "pr_number": 42},
                headers={"Idempotency-Key": "req-1"},
            ) as response,
        ):
            body = await response.aread()

        events = _parse_sse(body.decode())
        assert [name for name, _ in events] == ["review.completed"]

    async def test_rate_limit_exceeded_surfaces_as_a_failed_review_not_an_http_error(self) -> None:
        """The refusal is domain-level (Review.status=FAILED, an honest error message in the
        stream), not an HTTP error code -- the request to *ask* for a review succeeded; the
        review itself was refused. Conflating the two would make a client's retry logic guess
        whether 5xx means "try a different server" or "you already asked today"."""
        async with (
            httpx.AsyncClient(transport=make_app(rate_limit=0), base_url="http://test") as client,
            client.stream(
                "POST", "/api/reviews", json={"repo": "acme/widget", "pr_number": 42}
            ) as response,
        ):
            assert response.status_code == 200
            body = await response.aread()

        events = _parse_sse(body.decode())
        assert [name for name, _ in events] == ["review.completed"]
        assert events[0][1]["status"] == "failed"
