"""The live-review rate limiter, Postgres edition. Same behaviour and the same fixed bug as
``persistence/rate_limiter.py`` (SQLite) -- see that module's docstring for why a plain
INSERT ... ON CONFLICT DO UPDATE ... WHERE is not equivalent to seed-then-guarded-UPDATE
when the WHERE clause is the only thing enforcing the limit.
"""

from __future__ import annotations

import asyncio
from typing import Self

from app.domain.ports import ClockPort
from app.infrastructure.persistence.reconnecting import ReconnectingConnection

SCHEMA = """
CREATE TABLE IF NOT EXISTS live_review_counter (
    usage_date DATE PRIMARY KEY,
    count      INTEGER NOT NULL DEFAULT 0
);
"""


class PostgresRateLimiter:
    """Adapter satisfying ``RateLimiterPort``."""

    def __init__(self, *, limit: int, clock: ClockPort, connection: ReconnectingConnection) -> None:
        self._limit = limit
        self._clock = clock
        self._connection = connection

    @classmethod
    async def connect(cls, dsn: str, *, limit: int, clock: ClockPort) -> Self:
        connection = await asyncio.to_thread(cls._connect_sync, dsn)
        return cls(limit=limit, clock=clock, connection=connection)

    @staticmethod
    def _connect_sync(dsn: str) -> ReconnectingConnection:
        connection = ReconnectingConnection(dsn)
        with connection.cursor() as cursor:
            cursor.execute(SCHEMA)
        return connection

    async def try_acquire(self) -> bool:
        return await asyncio.to_thread(self._try_acquire_sync)

    def _try_acquire_sync(self) -> bool:
        today = self._clock.now().date()
        with self._connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO live_review_counter (usage_date, count) VALUES (%s, 0) "
                "ON CONFLICT (usage_date) DO NOTHING",
                (today,),
            )
            cursor.execute(
                "UPDATE live_review_counter SET count = count + 1 "
                "WHERE usage_date = %s AND count < %s "
                "RETURNING count",
                (today, self._limit),
            )
            row = cursor.fetchone()
        return row is not None

    async def close(self) -> None:
        await asyncio.to_thread(self._connection.close)
