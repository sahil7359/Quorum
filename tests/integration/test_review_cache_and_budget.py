"""The review cache and the daily token budget. Guardrails G9 and the cache half of G14.

Both are real SQLite, same reasoning as ``test_hitl_and_audit.py``: durability is the point,
so an in-memory fake would not exercise what actually matters -- that a value written now can
be read back, correctly reconstructed, after the connection outlives a single test's mental
model of "just a dict".
"""

from __future__ import annotations

from datetime import UTC, datetime

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
from app.infrastructure.persistence.budget import SqliteBudgetTracker
from app.infrastructure.persistence.review_cache import SqliteReviewCache
from tests.support.fakes import FrozenClock


def a_review(*, findings: tuple[Finding, ...] = (), head_sha: str = "head5678") -> Review:
    routing = RoutingDecision(
        specialists=(SpecialistKind.CORRECTNESS,),
        reason="correctness is always reviewed",
    )
    return Review(
        run_id=RunId.new(),
        repo=RepoRef.parse("acme/widget"),
        pr_number=42,
        head_sha=head_sha,
        status=RunStatus.PROPOSED,
        routing=routing,
        findings=findings,
        diff_truncated=False,
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
        file_path="app/auth/login.py",
        line_start=12,
    )


class TestReviewCache:
    async def test_miss_returns_none(self) -> None:
        cache = SqliteReviewCache()
        assert await cache.get("nonexistent") is None

    async def test_a_cached_review_round_trips_exactly(self) -> None:
        cache = SqliteReviewCache()
        review = a_review(findings=(a_finding(),))

        await cache.put("key-1", review)
        restored = await cache.get("key-1")

        assert restored is not None
        assert restored.run_id == review.run_id
        assert restored.repo == review.repo
        assert restored.pr_number == review.pr_number
        assert restored.head_sha == review.head_sha
        assert restored.status == review.status
        assert restored.routing == review.routing
        assert restored.started_at == review.started_at
        assert restored.finished_at == review.finished_at
        assert len(restored.findings) == 1
        assert restored.findings[0].finding_id == review.findings[0].finding_id
        assert restored.findings[0].citation.chunk_id == review.findings[0].citation.chunk_id
        assert restored.findings[0].citation.locator == review.findings[0].citation.locator

    async def test_an_empty_findings_review_round_trips(self) -> None:
        """The common case: cache-worthy because it cost real tokens, found nothing."""
        cache = SqliteReviewCache()
        review = a_review(findings=())

        await cache.put("key-empty", review)
        restored = await cache.get("key-empty")

        assert restored is not None
        assert restored.findings == ()

    async def test_writing_the_same_key_twice_keeps_the_latest(self) -> None:
        """A cache_key collision means the same (repo, pr, sha, config) was reviewed twice --
        the newer result is the one worth keeping, not something to reject."""
        cache = SqliteReviewCache()
        first = a_review(findings=())
        second = a_review(findings=(a_finding(),))

        await cache.put("key-1", first)
        await cache.put("key-1", second)
        restored = await cache.get("key-1")

        assert restored is not None
        assert restored.run_id == second.run_id
        assert len(restored.findings) == 1

    async def test_survives_a_real_connection_close_and_reopen(self, tmp_path: object) -> None:
        from pathlib import Path

        db_path = Path(str(tmp_path)) / "cache.sqlite"
        review = a_review()

        first = SqliteReviewCache(db_path)
        await first.put("key-1", review)
        first.close()

        second = SqliteReviewCache(db_path)
        restored = await second.get("key-1")
        assert restored is not None
        assert restored.run_id == review.run_id

    async def test_get_latest_returns_none_when_nothing_is_cached_for_this_pr(self) -> None:
        cache = SqliteReviewCache()
        assert await cache.get_latest(RepoRef.parse("acme/widget"), 42) is None

    async def test_get_latest_finds_a_review_cached_under_a_different_commit(self) -> None:
        """The budget-exhaustion fallback in docs/AppFlow.md doesn't know the current head_sha
        in advance -- that's the whole reason it exists instead of just calling get()."""
        cache = SqliteReviewCache()
        review = a_review(head_sha="old-sha")

        await cache.put("key-old", review)
        latest = await cache.get_latest(RepoRef.parse("acme/widget"), 42)

        assert latest is not None
        assert latest.head_sha == "old-sha"

    async def test_get_latest_prefers_the_most_recently_cached_commit(self) -> None:
        cache = SqliteReviewCache()
        older = a_review(head_sha="sha-older")
        newer = a_review(head_sha="sha-newer")

        await cache.put("key-older", older)
        await cache.put("key-newer", newer)
        latest = await cache.get_latest(RepoRef.parse("acme/widget"), 42)

        assert latest is not None
        assert latest.head_sha == "sha-newer"

    async def test_get_latest_does_not_cross_pull_requests(self) -> None:
        cache = SqliteReviewCache()
        review = a_review()
        await cache.put("key-1", review)

        assert await cache.get_latest(RepoRef.parse("acme/widget"), 99) is None


class TestBudget:
    async def test_no_usage_recorded_means_zero_consumed(self) -> None:
        tracker = SqliteBudgetTracker(limit=1000, clock=FrozenClock())
        state = await tracker.state()
        assert state.consumed == 0
        assert state.remaining == 1000
        assert not state.exhausted

    async def test_recorded_usage_sums_prompt_and_output_tokens(self) -> None:
        clock = FrozenClock()
        tracker = SqliteBudgetTracker(limit=1000, clock=clock)
        run_id = RunId.new()

        await tracker.record(
            run_id,
            TokenUsage(
                provider="ollama",
                model="llama3.1:8b",
                node="route",
                prompt_tokens=100,
                output_tokens=50,
                latency_ms=10,
            ),
        )
        await tracker.record(
            run_id,
            TokenUsage(
                provider="ollama",
                model="llama3.1:8b",
                node="correctness",
                prompt_tokens=200,
                output_tokens=75,
                latency_ms=20,
            ),
        )

        state = await tracker.state()
        assert state.consumed == 100 + 50 + 200 + 75

    async def test_exhaustion_is_derived_from_the_limit(self) -> None:
        clock = FrozenClock()
        tracker = SqliteBudgetTracker(limit=100, clock=clock)
        await tracker.record(
            RunId.new(),
            TokenUsage(
                provider="ollama",
                model="m",
                node="route",
                prompt_tokens=80,
                output_tokens=30,
                latency_ms=1,
            ),
        )
        state = await tracker.state()
        assert state.consumed == 110
        assert state.exhausted
        assert state.remaining == 0

    async def test_usage_from_a_different_day_does_not_count_against_today(
        self, tmp_path: object
    ) -> None:
        """The gate proof: a naive `SELECT SUM(...)` with no date filter would let yesterday's
        spend exhaust today's budget forever. This is the test that would catch that."""
        from pathlib import Path

        db_path = Path(str(tmp_path)) / "budget.sqlite"
        yesterday = FrozenClock(moment=datetime(2026, 8, 13, 23, 59, tzinfo=UTC))
        today = FrozenClock(moment=datetime(2026, 8, 14, 0, 1, tzinfo=UTC))

        tracker_yesterday = SqliteBudgetTracker(limit=1000, clock=yesterday, database=db_path)
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
        tracker_yesterday.close()

        # Same underlying file, different clock -- proves the filter is on usage_date, not on
        # "everything this adapter instance has ever seen".
        tracker_today = SqliteBudgetTracker(limit=1000, clock=today, database=db_path)
        state = await tracker_today.state()
        assert state.consumed == 0
