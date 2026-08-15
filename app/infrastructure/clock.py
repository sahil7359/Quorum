"""Real wall-clock time. Adapter satisfying ``ClockPort``.

Every other ``ClockPort`` use in this codebase has been a test fake (``FakeClock``) with a
value the test controls -- this is the first implementation that actually calls the system
clock, needed once something runs outside a test for the first time (the demo composition
root).
"""

from __future__ import annotations

from datetime import UTC, datetime


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)
