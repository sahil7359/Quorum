"""The daily token budget. Guardrail G9.

Derived from recorded fact, never a counter. ``state()`` sums every ``token_usage`` row for
today; there is no decrement operation to drift out of sync with what actually happened, which
is the failure mode a counter has the moment a call fails after the decrement but before the
response. See ``docs/Schema.md`` §8 and ``docs/AppFlow.md`` §3.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.domain.ports import ClockPort
from app.domain.values import BudgetState, RunId, TokenUsage

SCHEMA = """
CREATE TABLE IF NOT EXISTS token_usage (
    usage_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT    NOT NULL,
    node          TEXT    NOT NULL,
    provider      TEXT    NOT NULL,
    model         TEXT    NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    latency_ms    INTEGER NOT NULL,
    finish_reason TEXT,
    usage_date    TEXT    NOT NULL,
    created_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS token_usage_date_idx ON token_usage (usage_date);
"""


class SqliteBudgetTracker:
    """Adapter satisfying ``BudgetPort``.

    ``limit`` and the clock are constructor arguments, not read from ``Settings`` here --
    application code never sees configuration directly (Phase 0's layering rule), so the
    composition root passes both in explicitly.
    """

    def __init__(self, *, limit: int, clock: ClockPort, database: str | Path = ":memory:") -> None:
        self._limit = limit
        self._clock = clock
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(SCHEMA)
        self._connection.commit()

    async def state(self) -> BudgetState:
        today = self._clock.now().date().isoformat()
        row = self._connection.execute(
            "SELECT COALESCE(SUM(prompt_tokens + output_tokens), 0) AS consumed "
            "FROM token_usage WHERE usage_date = ?",
            (today,),
        ).fetchone()
        return BudgetState(consumed=int(row["consumed"]), limit=self._limit)

    async def record(self, run_id: RunId, usage: TokenUsage) -> None:
        now = self._clock.now()
        self._connection.execute(
            "INSERT INTO token_usage "
            "(run_id, node, provider, model, prompt_tokens, output_tokens, latency_ms, "
            "finish_reason, usage_date, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(run_id),
                usage.node,
                usage.provider,
                usage.model,
                usage.prompt_tokens,
                usage.output_tokens,
                usage.latency_ms,
                usage.finish_reason,
                now.date().isoformat(),
                now.isoformat(),
            ),
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()
