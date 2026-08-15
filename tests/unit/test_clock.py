from __future__ import annotations

from datetime import UTC

from app.infrastructure.clock import SystemClock


def test_now_returns_a_timezone_aware_utc_timestamp() -> None:
    clock = SystemClock()
    moment = clock.now()
    assert moment.tzinfo is UTC


def test_now_advances() -> None:
    clock = SystemClock()
    first = clock.now()
    second = clock.now()
    assert second >= first
