"""The live-review rate limiter. Guardrail-adjacent to G9, but a distinct cap -- see
``RateLimiterPort``'s docstring for why request volume and token spend are tracked separately.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.domain.ports import ClockPort

SCHEMA = """
CREATE TABLE IF NOT EXISTS live_review_counter (
    usage_date TEXT PRIMARY KEY,
    count      INTEGER NOT NULL DEFAULT 0
);
"""


class SqliteRateLimiter:
    """Adapter satisfying ``RateLimiterPort``."""

    def __init__(self, *, limit: int, clock: ClockPort, database: str | Path = ":memory:") -> None:
        self._limit = limit
        self._clock = clock
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(SCHEMA)
        self._connection.commit()

    async def try_acquire(self) -> bool:
        today = self._clock.now().date().isoformat()
        # why: seeding the row at 0 first, then always going through the same WHERE-guarded
        #      UPDATE, means a limit of 0 is respected on the very first call of the day too.
        #      A single "INSERT ... VALUES (?, 1) ON CONFLICT DO UPDATE ... WHERE count < ?"
        #      looked equivalent and was not: its plain-INSERT branch (no existing row) always
        #      succeeds with count=1 regardless of the limit, so limit=0 let the first request
        #      through -- caught by test_limit_of_zero_refuses_immediately, which is exactly
        #      the gate-proof this file needed.
        self._connection.execute(
            "INSERT OR IGNORE INTO live_review_counter (usage_date, count) VALUES (?, 0)",
            (today,),
        )
        cursor = self._connection.execute(
            "UPDATE live_review_counter SET count = count + 1 "
            "WHERE usage_date = ? AND count < ? "
            "RETURNING count",
            (today, self._limit),
        )
        row = cursor.fetchone()
        self._connection.commit()
        return row is not None

    def close(self) -> None:
        self._connection.close()
