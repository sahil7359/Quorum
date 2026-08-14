"""The append-only audit log, Postgres edition.

Mirrors ``persistence/audit.py`` exactly in behaviour and API; the one thing that changes is
*how* append-only is enforced. SQLite has no ``RULE`` system, so that adapter uses ``BEFORE
UPDATE``/``BEFORE DELETE`` triggers that ``RAISE(ABORT, ...)``. Postgres's documented mechanism
is different -- ``CREATE RULE ... DO INSTEAD NOTHING``, which silently turns the write into a
no-op rather than raising -- and ``docs/Schema.md`` §6 specifies that mechanism, not the
SQLite one. HANDOFF.md flagged this explicitly as a risk the SQLite green did not cover:
"the append-only rules have never executed" against a real Postgres. This module, and the two
tests it re-runs (``test_update_is_refused_by_the_database``,
``test_delete_is_refused_by_the_database``, both in
``tests/integration/test_postgres_audit.py``), close that gap.

**Behavioural difference worth knowing:** a ``RULE ... DO INSTEAD NOTHING`` makes the write
report success (``UPDATE 0`` rather than an error) rather than raising. The SQLite trigger
raises. Both achieve "the row cannot change," but a caller checking "did my UPDATE raise"
would get a different answer here than against SQLite -- this adapter's own test suite checks
the row is unchanged, not that an exception was thrown, precisely because that is the
Postgres-true assertion.

**Sync driver, wrapped in a thread.** ``psycopg``'s async mode refuses to run under Windows'
default ``ProactorEventLoop`` (needs ``SelectorEventLoop``), and the rest of this codebase's
async tests -- the MCP stdio subprocess transport in particular -- need ``ProactorEventLoop``
on Windows. Rather than force a global event-loop-policy choice that would trade one adapter's
convenience for another's subprocess support, this wraps the ordinary blocking ``psycopg``
client in ``asyncio.to_thread``. Correct and portable everywhere, including the Linux
production target, where the distinction does not even arise.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any, Self

import psycopg
from psycopg.rows import dict_row

from app.domain.entities import Approval, AuditEvent
from app.domain.values import ApprovalAction, AuditAction, FindingId, RunId

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    audit_id     BIGSERIAL PRIMARY KEY,
    run_id       TEXT        NOT NULL,
    finding_id   TEXT,
    action       TEXT        NOT NULL,
    actor        TEXT        NOT NULL,
    payload_hash TEXT,
    detail       JSONB       NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS audit_events_run_idx ON audit_events (run_id, audit_id);

-- Append-only, enforced by the database rather than by convention -- docs/Schema.md §6's
-- documented mechanism, distinct from SQLite's trigger-based one. DROP/CREATE rather than
-- CREATE OR REPLACE: Postgres has no CREATE OR REPLACE RULE, and a plain CREATE RULE errors
-- if the migration runs twice (schema setup here is idempotent-by-reconnect, same as the
-- SQLite adapter's CREATE TABLE IF NOT EXISTS).
DROP RULE IF EXISTS audit_events_no_update ON audit_events;
CREATE RULE audit_events_no_update AS ON UPDATE TO audit_events DO INSTEAD NOTHING;

DROP RULE IF EXISTS audit_events_no_delete ON audit_events;
CREATE RULE audit_events_no_delete AS ON DELETE TO audit_events DO INSTEAD NOTHING;
"""


class PostgresAuditLog:
    """Adapter satisfying ``AuditPort``. Append-only by database rule, not by discipline."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    @classmethod
    async def connect(cls, dsn: str) -> Self:
        return await asyncio.to_thread(cls._connect_sync, dsn)

    @classmethod
    def _connect_sync(cls, dsn: str) -> Self:
        # why: autocommit -- each audit append is its own durable fact the instant it is
        #      written, not something that should wait behind an application-level
        #      transaction boundary this class does not otherwise manage.
        connection = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        with connection.cursor() as cursor:
            cursor.execute(SCHEMA)
        return cls(connection)

    async def append(self, event: AuditEvent) -> None:
        await asyncio.to_thread(self._append_sync, event)

    def _append_sync(self, event: AuditEvent) -> None:
        created = (event.created_at or datetime.now(UTC)).isoformat()
        with self._connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO audit_events "
                "(run_id, finding_id, action, actor, payload_hash, detail, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    str(event.run_id),
                    str(event.finding_id) if event.finding_id else None,
                    event.action.value,
                    event.actor,
                    event.payload_hash,
                    json.dumps(event.detail, sort_keys=True),
                    created,
                ),
            )

    async def history(self, run_id: RunId) -> list[AuditEvent]:
        return await asyncio.to_thread(self._history_sync, run_id)

    def _history_sync(self, run_id: RunId) -> list[AuditEvent]:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM audit_events WHERE run_id = %s ORDER BY audit_id", (str(run_id),)
            )
            rows = cursor.fetchall()
        return [_to_event(row) for row in rows]

    async def approval_for(self, finding_id: FindingId) -> Approval | None:
        return await asyncio.to_thread(self._approval_for_sync, finding_id)

    def _approval_for_sync(self, finding_id: FindingId) -> Approval | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM audit_events WHERE finding_id = %s "
                "AND action IN ('approved', 'rejected') ORDER BY audit_id DESC LIMIT 1",
                (str(finding_id),),
            )
            row = cursor.fetchone()
        if row is None:
            return None

        return Approval(
            run_id=RunId(row["run_id"]),
            finding_id=FindingId(row["finding_id"]),
            action=ApprovalAction(row["action"]),
            actor=row["actor"],
            payload_hash=row["payload_hash"] or "",
            created_at=row["created_at"],
        )

    async def close(self) -> None:
        await asyncio.to_thread(self._connection.close)


def _to_event(row: dict[str, Any]) -> AuditEvent:
    detail = row["detail"]
    return AuditEvent(
        run_id=RunId(row["run_id"]),
        action=AuditAction(row["action"]),
        actor=row["actor"],
        finding_id=FindingId(row["finding_id"]) if row["finding_id"] else None,
        payload_hash=row["payload_hash"],
        detail=detail if isinstance(detail, dict) else json.loads(detail),
        created_at=row["created_at"],
    )
