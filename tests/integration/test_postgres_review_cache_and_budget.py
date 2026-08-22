"""The review cache, budget tracker, and rate limiter against real Postgres.

Same suite shape as ``test_review_cache_and_budget.py`` (SQLite) and ``test_rate_limiter.py`` --
this file exists to prove the Postgres adapters behave identically, not to re-derive new
behaviour. Needs a real Postgres reachable at ``QUORUM_TEST_DATABASE_URL`` (defaults to the
local ``quorum-postgres`` Docker container on port 5433).
"""

from __future__ import annotations

import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.entities import Citation, Finding, RepoRef, Review, RoutingDecision
from app.domain.values import (
    ChunkId,
    ChunkLocator,
    FindingId,
    RunId,
    RunStatus,
    Severity,
    SpecialistKind,
    TokenUsage,
)
from app.infrastructure.persistence.postgres_budget import PostgresBudgetTracker
from app.infrastructure.persistence.postgres_rate_limiter import PostgresRateLimiter
from app.infrastructure.persistence.postgres_review_cache import PostgresReviewCache
from tests.support.fakes import FrozenClock

pytestmark = pytest.mark.integration

DSN = os.environ.get("QUORUM_TEST_DATABASE_URL", "postgresql://quorum:quorum@localhost:5433/quorum")


def _unique_day() -> datetime:
    """A day this test invocation almost certainly owns alone.

    why: budget and rate-limit state is scoped by calendar day in a *persistent* real
    database -- unlike the SQLite adapters, there is no fresh in-memory database per test.
    A fixed literal date (2020-01-01) isolates this test from its siblings within one run,
    but not from itself across repeated manual runs against the same container, which is
    exactly what broke it live: the second run in this session saw the first run's rows and
    asserted a stale total. Deriving the day from a UUID instead of a constant means each
    invocation gets its own slot without needing a truncate-before-suite fixture.
    """
    offset_days = uuid.uuid4().int % 3650
    return datetime(2000, 1, 1, tzinfo=UTC) + timedelta(days=offset_days)


def a_review(*, findings: tuple[Finding, ...] = (), head_sha: str = "head5678") -> Review:
    routing = RoutingDecision(
        specialists=(SpecialistKind.CORRECTNESS,), reason="correctness is always reviewed"
    )
    return Review(
        run_id=RunId.new(),
        repo=RepoRef.parse("acme/widget"),
        pr_number=42,
        head_sha=head_sha,
        status=RunStatus.PROPOSED,
        routing=routing,
        findings=findings,
        started_at=datetime(2026, 8, 14, 12, 0, tzinfo=UTC),
        finished_at=datetime(2026, 8, 14, 12, 1, tzinfo=UTC),
    )


def a_finding() -> Finding:
    locator = ChunkLocator(
        repo="acme/widget",
        commit_sha="head5678",
        file_path="docs/security.md",
        section_path="Sessions > Expiry",
        start_offset=0,
        end_offset=200,
    )
    return Finding(
        finding_id=FindingId.new(),
        specialist=SpecialistKind.SECURITY,
        severity=Severity.HIGH,
        confidence=0.9,
        title="Token never expires",
        body="Tokens must carry an expiry.",
        citation=Citation(chunk_id=ChunkId.derive(locator), locator=locator, quote="excerpt"),
    )


class TestConnectionResilience:
    async def test_it_reconnects_after_the_connection_is_closed_underneath_it(self) -> None:
        """Found live: Neon closes an idle connection and every query then fails with 'the
        connection is closed' until the process restarts. Simulate exactly that -- close the
        underlying psycopg connection out from under the adapter -- and assert the next
        operation still succeeds rather than raising."""
        cache = await PostgresReviewCache.connect(DSN)
        review = a_review(findings=(a_finding(),), head_sha="reconnect")
        key = f"reconnect-{review.run_id}"
        await cache.put(key, review)

        # Reach in and close the real connection, the way Neon's idle timeout does.
        cache._connection._conn.close()
        assert cache._connection._conn.closed

        # The adapter should transparently reopen and serve the row, not raise.
        restored = await cache.get(key)
        assert restored is not None
        assert restored.run_id == review.run_id
        await cache.close()


class TestReviewCache:
    async def test_a_cached_review_round_trips(self) -> None:
        cache = await PostgresReviewCache.connect(DSN)
        review = a_review(findings=(a_finding(),), head_sha="rc-roundtrip")
        key = f"key-{review.run_id}"

        await cache.put(key, review)
        restored = await cache.get(key)

        assert restored is not None
        assert restored.run_id == review.run_id
        assert len(restored.findings) == 1
        assert restored.findings[0].citation.chunk_id == review.findings[0].citation.chunk_id
        await cache.close()

    async def test_get_latest_prefers_the_most_recent_commit(self) -> None:
        cache = await PostgresReviewCache.connect(DSN)
        repo = RepoRef.parse("acme/widget-latest")
        older = a_review(head_sha="older")
        newer = a_review(head_sha="newer")

        await cache.put(f"key-{older.run_id}", replace(older, repo=repo))
        await cache.put(f"key-{newer.run_id}", replace(newer, repo=repo))

        latest = await cache.get_latest(repo, 42)
        assert latest is not None
        assert latest.head_sha == "newer"
        await cache.close()


class TestBudget:
    async def test_recorded_usage_sums_for_today(self) -> None:
        clock = FrozenClock()
        tracker = await PostgresBudgetTracker.connect(DSN, limit=1000, clock=clock)
        run_id = RunId.new()

        await tracker.record(
            run_id,
            TokenUsage(
                provider="ollama",
                model="m",
                node="route",
                prompt_tokens=100,
                output_tokens=50,
                latency_ms=1,
            ),
        )
        state_before = await tracker.state()

        await tracker.record(
            run_id,
            TokenUsage(
                provider="ollama",
                model="m",
                node="correctness",
                prompt_tokens=200,
                output_tokens=75,
                latency_ms=1,
            ),
        )
        state_after = await tracker.state()

        assert state_after.consumed == state_before.consumed + 275
        await tracker.close()

    async def test_usage_from_a_different_day_does_not_count_against_today(self) -> None:
        yesterday = FrozenClock(moment=_unique_day())
        tracker_yesterday = await PostgresBudgetTracker.connect(DSN, limit=1000, clock=yesterday)
        await tracker_yesterday.record(
            RunId.new(),
            TokenUsage(
                provider="ollama",
                model="m",
                node="route",
                prompt_tokens=900,
                output_tokens=0,
                latency_ms=1,
            ),
        )
        state = await tracker_yesterday.state()
        assert state.consumed == 900  # this test's own unique day, isolated from every other
        await tracker_yesterday.close()


class TestRateLimiter:
    async def test_the_call_that_exceeds_the_limit_is_refused(self) -> None:
        clock = FrozenClock(moment=_unique_day())
        limiter = await PostgresRateLimiter.connect(DSN, limit=2, clock=clock)

        assert await limiter.try_acquire()
        assert await limiter.try_acquire()
        assert not await limiter.try_acquire()
        await limiter.close()

    async def test_limit_of_zero_refuses_immediately(self) -> None:
        clock = FrozenClock(moment=_unique_day())
        limiter = await PostgresRateLimiter.connect(DSN, limit=0, clock=clock)

        assert not await limiter.try_acquire()
        await limiter.close()
