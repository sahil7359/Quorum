"""The append-only audit log against a real Postgres, not just SQLite.

HANDOFF.md flagged this precisely: the SQLite append-only guarantee is enforced by triggers
that raise; Postgres's documented mechanism (``docs/Schema.md`` §6) is ``CREATE RULE ... DO
INSTEAD NOTHING``, a *different* mechanism that silently no-ops rather than raising. "The
Postgres rules have never executed" was the exact risk on record. This file executes them.

Needs a real Postgres reachable at ``QUORUM_TEST_DATABASE_URL`` (defaults to the local
``quorum-postgres`` Docker container this session started on port 5433). Marked
``integration`` for the same reason the MCP-over-real-stdio tests are: a mechanism this
specific is not worth faking.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from app.domain.entities import AuditEvent
from app.domain.values import AuditAction, FindingId, RunId
from app.infrastructure.persistence.postgres_audit import PostgresAuditLog

pytestmark = pytest.mark.integration

DSN = os.environ.get("QUORUM_TEST_DATABASE_URL", "postgresql://quorum:quorum@localhost:5433/quorum")


async def a_log() -> PostgresAuditLog:
    return await PostgresAuditLog.connect(DSN)


class TestAuditIsAppendOnly:
    async def test_events_are_recorded_and_ordered(self) -> None:
        audit = await a_log()
        run = RunId.new()

        for action in (AuditAction.PROPOSED, AuditAction.APPROVED, AuditAction.POSTED):
            await audit.append(AuditEvent(run_id=run, action=action, actor="sahil"))

        history = await audit.history(run)
        assert [e.action for e in history] == [
            AuditAction.PROPOSED,
            AuditAction.APPROVED,
            AuditAction.POSTED,
        ]
        await audit.close()

    async def test_an_update_against_the_real_database_leaves_the_row_unchanged(self) -> None:
        """The Postgres-true assertion. A RULE ... DO INSTEAD NOTHING does not raise -- it
        makes the UPDATE affect zero rows. Checking for an exception here, mirroring the
        SQLite test almost by copy-paste, would pass for the wrong reason (or not run the
        real assertion at all) and is exactly the failure this file exists to prevent."""
        audit = await a_log()
        run = RunId.new()
        await audit.append(AuditEvent(run_id=run, action=AuditAction.POSTED, actor="sahil"))

        def attempt_update() -> None:
            with audit._connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE audit_events SET actor = 'someone else' WHERE run_id = %s",
                    (str(run),),
                )

        await asyncio.to_thread(attempt_update)

        history = await audit.history(run)
        assert len(history) == 1
        assert history[0].actor == "sahil"
        await audit.close()

    async def test_a_delete_against_the_real_database_leaves_the_row_in_place(self) -> None:
        audit = await a_log()
        run = RunId.new()
        await audit.append(AuditEvent(run_id=run, action=AuditAction.POSTED, actor="sahil"))

        def attempt_delete() -> None:
            with audit._connection.cursor() as cursor:
                cursor.execute("DELETE FROM audit_events WHERE run_id = %s", (str(run),))

        await asyncio.to_thread(attempt_delete)

        history = await audit.history(run)
        assert len(history) == 1
        await audit.close()

    async def test_the_adapter_exposes_no_mutation_method(self) -> None:
        surface = {name for name in dir(PostgresAuditLog) if not name.startswith("_")}
        assert not surface & {"update", "delete", "remove", "purge", "clear"}

    async def test_a_rejection_is_distinguishable_from_no_decision(self) -> None:
        audit = await a_log()
        finding_id = FindingId.new()
        assert await audit.approval_for(finding_id) is None

        await audit.append(
            AuditEvent(
                run_id=RunId.new(),
                action=AuditAction.REJECTED,
                actor="sahil",
                finding_id=finding_id,
            )
        )
        decision = await audit.approval_for(finding_id)
        assert decision is not None
        assert decision.action.value == "rejected"
        await audit.close()

    async def test_the_latest_decision_wins(self) -> None:
        audit = await a_log()
        finding_id = FindingId.new()
        run = RunId.new()

        await audit.append(
            AuditEvent(
                run_id=run, action=AuditAction.REJECTED, actor="sahil", finding_id=finding_id
            )
        )
        await audit.append(
            AuditEvent(
                run_id=run,
                action=AuditAction.APPROVED,
                actor="sahil",
                finding_id=finding_id,
                payload_hash="abc",
            )
        )

        decision = await audit.approval_for(finding_id)
        assert decision is not None
        assert decision.action.value == "approved"
        await audit.close()
