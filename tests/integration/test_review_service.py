"""The composition root's request lifecycle, with fakes at every port.

Mirrors ``test_review_graph.py``'s fixtures (the graph itself is already proven there); this
file proves the machinery *around* the graph -- cache, budget, rate limit, idempotency -- makes
the right call about whether to run it at all.
"""

from __future__ import annotations

import asyncio
import json

from app.domain.entities import (
    ChangedFile,
    Chunk,
    Diff,
    DiffHunk,
    PullRequest,
    RepoRef,
    ScoredChunk,
)
from app.domain.values import ChunkLocator, RunStatus
from app.infrastructure.persistence.budget import SqliteBudgetTracker
from app.infrastructure.persistence.rate_limiter import SqliteRateLimiter
from app.infrastructure.persistence.review_cache import SqliteReviewCache
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

SOURCE = """def authenticate(user, password):
    if not user:
        return None
    token = issue_token(user)
    token.expires_at = None
    return token
"""


def a_diff() -> Diff:
    hunk = DiffHunk(
        "app/auth/login.py", 1, 6, 1, 6, "+    token.expires_at = None\n+    return token"
    )
    return Diff(
        files=(ChangedFile("app/auth/login.py", "modified", 2, 0, (hunk,)),),
        raw="diff --git a/app/auth/login.py b/app/auth/login.py\n",
    )


def a_chunk() -> Chunk:
    return Chunk.create(
        locator=ChunkLocator(
            repo="acme/widget",
            commit_sha="head5678",
            file_path="docs/security.md",
            section_path="Sessions > Expiry",
            start_offset=0,
            end_offset=200,
        ),
        content="All issued tokens must carry an expiry.",
        start_line=1,
        end_line=5,
        ordinal=0,
        token_count=40,
    )


def findings_json(chunk_id: str) -> str:
    return json.dumps(
        {
            "findings": [
                {
                    "title": "Token never expires",
                    "body": "Tokens must carry an expiry.",
                    "severity": "high",
                    "confidence": 0.9,
                    "chunk_id": chunk_id,
                    "file_path": "app/auth/login.py",
                    "line_start": 5,
                }
            ]
        }
    )


def a_code_host(*, head_sha: str = "head5678") -> FakeCodeHost:
    return FakeCodeHost(
        pull_request=PullRequest(
            repo=REPO,
            number=42,
            title="Issue tokens on login",
            body="",
            author="octocat",
            base_sha="base1234",
            head_sha=head_sha,
        ),
        diff=a_diff(),
        files={"app/auth/login.py": SOURCE},
    )


def make_service(
    *,
    code_host: FakeCodeHost | None = None,
    model: FakeChatModel | None = None,
    retriever: FakeRetriever | None = None,
    cache: SqliteReviewCache | None = None,
    budget_limit: int = 1_000_000,
    rate_limit: int = 1000,
    clock: FrozenClock | None = None,
) -> ReviewService:
    clock = clock or FrozenClock()
    return ReviewService(
        code_host=code_host or a_code_host(),
        route_model=model or FakeChatModel(),
        specialist_model=model or FakeChatModel(),
        retriever=retriever or FakeRetriever(),
        cache=cache or SqliteReviewCache(),
        budget=SqliteBudgetTracker(limit=budget_limit, clock=clock),
        rate_limiter=SqliteRateLimiter(limit=rate_limit, clock=clock),
        clock=clock,
        logger=RecordingLogger(),
        tracer=NullTracer(),
        config_hash="test-config-hash",
        max_diff_lines=1500,
        retrieval_top_k=5,
    )


class TestHappyPath:
    async def test_a_fresh_review_runs_the_graph_and_caches_the_result(self) -> None:
        chunk = a_chunk()
        model = FakeChatModel(
            responses={
                "route": ['{"specialists": ["correctness", "security"], "reason": "auth path"}'],
                "security": [findings_json(str(chunk.chunk_id))],
            }
        )
        cache = SqliteReviewCache()
        service = make_service(
            model=model,
            retriever=FakeRetriever(chunks=[ScoredChunk(chunk=chunk, score=0.9)]),
            cache=cache,
        )

        review = await service.review(REPO, 42)

        assert review.status == RunStatus.PROPOSED
        assert len(review.findings) == 1
        assert review.error is None

        # It was actually cached, not just returned.
        cache_key = service._cache_key(REPO, 42, "head5678")
        assert await cache.get(cache_key) is not None

    async def test_a_cache_hit_never_touches_the_model(self) -> None:
        model = FakeChatModel()
        cache = SqliteReviewCache()
        service = make_service(model=model, cache=cache)

        first = await service.review(REPO, 42)
        model.calls.clear()
        second = await service.review(REPO, 42)

        assert second.run_id == first.run_id
        assert model.calls == []


class TestRateLimit:
    async def test_exceeding_the_limit_refuses_without_running_the_graph(self) -> None:
        model = FakeChatModel()
        service = make_service(model=model, rate_limit=0)

        review = await service.review(REPO, 42)

        assert review.status == RunStatus.FAILED
        assert review.error is not None
        assert "rate limit" in review.error
        assert model.calls == []

    async def test_a_cache_hit_bypasses_the_rate_limit(self) -> None:
        """A cached gallery keeps working even at zero live-review budget -- docs/Security.md."""
        cache = SqliteReviewCache()
        service = make_service(cache=cache, rate_limit=1000)
        await service.review(REPO, 42)

        exhausted = make_service(cache=cache, rate_limit=0)
        review = await exhausted.review(REPO, 42)

        assert review.status == RunStatus.PROPOSED
        assert review.error is None


class TestBudget:
    async def test_exhaustion_with_no_cached_review_is_refused_honestly(self) -> None:
        service = make_service(budget_limit=0)
        review = await service.review(REPO, 42)

        assert review.status == RunStatus.FAILED
        assert review.error is not None
        assert "budget" in review.error

    async def test_exhaustion_with_a_cached_review_serves_it_with_a_banner(self) -> None:
        cache = SqliteReviewCache()
        fresh = make_service(cache=cache, budget_limit=1_000_000)
        original = await fresh.review(REPO, 42)
        assert original.error is None

        # A different PR's cache_key (different head_sha) so the exact-key cache misses and
        # the budget-exhaustion fallback path -- get_latest -- is what has to find it.
        exhausted = make_service(
            cache=cache, budget_limit=0, code_host=a_code_host(head_sha="new-sha")
        )
        review = await exhausted.review(REPO, 42)

        assert review.status == RunStatus.PROPOSED
        assert review.findings == original.findings
        assert review.error is not None
        assert "budget exhausted" in review.error


class TestIdempotency:
    async def test_concurrent_requests_with_the_same_key_run_the_graph_once(self) -> None:
        model = FakeChatModel(
            responses={"route": ['{"specialists": ["correctness"], "reason": "x"}']}
        )
        service = make_service(model=model)

        results = await asyncio.gather(
            service.review(REPO, 42, idempotency_key="dup-1"),
            service.review(REPO, 42, idempotency_key="dup-1"),
        )

        assert results[0].run_id == results[1].run_id
        route_calls = [c for c in model.calls if c[0] == "route"]
        assert len(route_calls) == 1

    async def test_different_keys_both_run(self) -> None:
        model = FakeChatModel(
            responses={"route": ['{"specialists": ["correctness"], "reason": "x"}']}
        )
        cache = SqliteReviewCache()
        service = make_service(model=model, cache=cache)

        await service.review(REPO, 42, idempotency_key="key-a")
        # cache now has this PR's review, so a second call under a different key would hit the
        # cache rather than run again -- prove the *coalescing* behaviour instead, with two
        # different in-flight requests before either has cached anything.
        service2 = make_service(model=model, cache=SqliteReviewCache())
        results = await asyncio.gather(
            service2.review(REPO, 42, idempotency_key="key-b"),
            service2.review(REPO, 43, idempotency_key="key-c"),
        )
        assert results[0].pr_number == 42
        assert results[1].pr_number == 43


class TestStreaming:
    async def test_a_fresh_review_yields_one_event_per_node_then_completes(self) -> None:
        chunk = a_chunk()
        model = FakeChatModel(
            responses={
                "route": ['{"specialists": ["correctness", "security"], "reason": "auth path"}'],
                "security": [findings_json(str(chunk.chunk_id))],
            }
        )
        service = make_service(
            model=model, retriever=FakeRetriever(chunks=[ScoredChunk(chunk=chunk, score=0.9)])
        )

        events = [event async for event in service.review_stream(REPO, 42)]
        names = [name for name, _ in events]

        assert names == [
            "node.ingest",
            "node.route",
            "node.specialists",
            "node.synthesise",
            "review.completed",
        ]

        route_event = dict(events)["node.route"]
        assert "security" in route_event["specialists"]

        final = dict(events)["review.completed"]
        assert final["status"] == "proposed"
        assert len(final["findings"]) == 1
        assert final["posted_to_github"] is False

    async def test_a_cache_hit_yields_a_single_completed_event(self) -> None:
        cache = SqliteReviewCache()
        service = make_service(cache=cache)
        await service.review(REPO, 42)

        events = [event async for event in service.review_stream(REPO, 42)]

        assert [name for name, _ in events] == ["review.completed"]
        assert events[0][1]["status"] == "proposed"

    async def test_a_refused_stream_yields_a_single_completed_event_with_an_error(self) -> None:
        service = make_service(rate_limit=0)

        events = [event async for event in service.review_stream(REPO, 42)]

        assert [name for name, _ in events] == ["review.completed"]
        payload = events[0][1]
        assert payload["status"] == "failed"
        assert payload["error"] is not None
