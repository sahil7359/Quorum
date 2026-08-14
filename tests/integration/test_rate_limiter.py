"""The live-review rate limiter.

The gate-proof rule applies here too: a limiter that cannot be shown refusing the 4th call is
not a tested limiter, it's a table that happens to have a limit column.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.infrastructure.persistence.rate_limiter import SqliteRateLimiter
from tests.support.fakes import FrozenClock


class TestRateLimiter:
    async def test_calls_under_the_limit_succeed(self) -> None:
        limiter = SqliteRateLimiter(limit=3, clock=FrozenClock())
        assert await limiter.try_acquire()
        assert await limiter.try_acquire()
        assert await limiter.try_acquire()

    async def test_the_call_that_exceeds_the_limit_is_refused(self) -> None:
        limiter = SqliteRateLimiter(limit=2, clock=FrozenClock())
        assert await limiter.try_acquire()
        assert await limiter.try_acquire()
        assert not await limiter.try_acquire()

    async def test_refusal_does_not_consume_a_slot(self) -> None:
        """A refused call must not itself count against the cap, or the limiter would
        eventually refuse everything even at zero real traffic."""
        limiter = SqliteRateLimiter(limit=1, clock=FrozenClock())
        assert await limiter.try_acquire()
        assert not await limiter.try_acquire()
        assert not await limiter.try_acquire()

    async def test_limit_of_zero_refuses_immediately(self) -> None:
        limiter = SqliteRateLimiter(limit=0, clock=FrozenClock())
        assert not await limiter.try_acquire()

    async def test_the_cap_resets_on_a_new_day(self) -> None:
        yesterday = FrozenClock(moment=datetime(2026, 8, 13, 23, 59, tzinfo=UTC))
        limiter = SqliteRateLimiter(limit=1, clock=yesterday)
        assert await limiter.try_acquire()
        assert not await limiter.try_acquire()

        limiter._clock = FrozenClock(moment=datetime(2026, 8, 14, 0, 1, tzinfo=UTC))
        assert await limiter.try_acquire()

    async def test_survives_a_real_connection_close_and_reopen(self, tmp_path: Path) -> None:
        db_path = tmp_path / "limiter.sqlite"
        clock = FrozenClock()

        first = SqliteRateLimiter(limit=1, clock=clock, database=db_path)
        assert await first.try_acquire()
        first.close()

        second = SqliteRateLimiter(limit=1, clock=clock, database=db_path)
        assert not await second.try_acquire()
