"""The daily token budget, Postgres edition. Same behaviour as ``persistence/budget.py``
(SQLite); see ``postgres_review_cache.py``'s module docstring for why a Postgres version
exists (Render's free-tier disk is ephemeral) and ``postgres_audit.py``'s for why sync
psycopg wrapped in threads rather than psycopg's async mode.
"""

from __future__ import annotations

import asyncio
from typing import Any, Self

from app.domain.ports import ClockPort
from app.domain.values import BudgetState, RunId, TokenUsage
from app.infrastructure.persistence.reconnecting import ReconnectingConnection

SCHEMA = """
CREATE TABLE IF NOT EXISTS token_usage (
    usage_id      BIGSERIAL PRIMARY KEY,
    run_id        TEXT        NOT NULL,
    node          TEXT        NOT NULL,
    provider      TEXT        NOT NULL,
    model         TEXT        NOT NULL,
    prompt_tokens INTEGER     NOT NULL,
    output_tokens INTEGER     NOT NULL,
    latency_ms    INTEGER     NOT NULL,
    finish_reason TEXT,
    usage_date    DATE        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS token_usage_date_idx ON token_usage (usage_date);
"""


class PostgresBudgetTracker:
    """Adapter satisfying ``BudgetPort``."""

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

    async def state(self) -> BudgetState:
        today = self._clock.now().date()
        return await asyncio.to_thread(self._state_sync, today)

    def _state_sync(self, today: Any) -> BudgetState:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT COALESCE(SUM(prompt_tokens + output_tokens), 0) AS consumed "
                "FROM token_usage WHERE usage_date = %s",
                (today,),
            )
            row = cursor.fetchone()
        # An aggregate query with no GROUP BY always returns exactly one row, even when zero
        # underlying rows matched (COALESCE turns that into 0) -- the assert documents that
        # this is a formality for the type checker, not a real "what if it's empty" case.
        assert row is not None
        return BudgetState(consumed=int(row["consumed"]), limit=self._limit)

    async def record(self, run_id: RunId, usage: TokenUsage) -> None:
        await asyncio.to_thread(self._record_sync, run_id, usage)

    def _record_sync(self, run_id: RunId, usage: TokenUsage) -> None:
        now = self._clock.now()
        with self._connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO token_usage "
                "(run_id, node, provider, model, prompt_tokens, output_tokens, latency_ms, "
                "finish_reason, usage_date, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    str(run_id),
                    usage.node,
                    usage.provider,
                    usage.model,
                    usage.prompt_tokens,
                    usage.output_tokens,
                    usage.latency_ms,
                    usage.finish_reason,
                    now.date(),
                    now,
                ),
            )

    async def close(self) -> None:
        await asyncio.to_thread(self._connection.close)
