"""The append-only audit log.

**Audit is a table, not a log stream.** Logs rotate, and the answer to "why did it post that?"
must outlive log retention. It is never sampled and never deleted.

Append-only is enforced in two independent places:

1. **Database triggers** that raise on UPDATE and DELETE. Application code cannot bypass them
   by holding the connection directly.
2. **No update or delete method exists** on the adapter, so the application has no vocabulary
   for the operation.

SQLite here rather than Postgres. That is a deliberate scope decision, not an oversight: the
audit table needs durability and append-only semantics, both of which SQLite provides, and it
removes a service dependency from local development and CI. The Postgres adapter is a port
implementation away and the DDL below translates directly (`CREATE RULE ... DO INSTEAD
NOTHING` in Postgres, triggers here). See HANDOFF Phase 5.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from app.domain.entities import Approval, AuditEvent
from app.domain.values import ApprovalAction, AuditAction, FindingId, RunId

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    audit_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT    NOT NULL,
    finding_id   TEXT,
    action       TEXT    NOT NULL,
    actor        TEXT    NOT NULL,
    payload_hash TEXT,
    detail       TEXT    NOT NULL DEFAULT '{}',
    created_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS audit_events_run_idx ON audit_events (run_id, audit_id);

-- Append-only, enforced by the database rather than by convention. An application bug, or
-- someone with a connection and good intentions, cannot rewrite the approval trail.
CREATE TRIGGER IF NOT EXISTS audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only: UPDATE is refused');
END;

CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only: DELETE is refused');
END;
"""


class SqliteAuditLog:
    """Adapter satisfying ``AuditPort``. Append-only by construction."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(SCHEMA)
        self._connection.commit()

    async def append(self, event: AuditEvent) -> None:
        created = (event.created_at or datetime.now(UTC)).isoformat()
        self._connection.execute(
            "INSERT INTO audit_events "
            "(run_id, finding_id, action, actor, payload_hash, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
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
        self._connection.commit()

    async def history(self, run_id: RunId) -> Sequence[AuditEvent]:
        rows = self._connection.execute(
            "SELECT * FROM audit_events WHERE run_id = ? ORDER BY audit_id", (str(run_id),)
        ).fetchall()
        return [_to_event(row) for row in rows]

    async def approval_for(self, finding_id: FindingId) -> Approval | None:
        """The most recent approval decision for a finding.

        Ordered by ``audit_id`` descending so a re-approval after an edit wins over the
        original. Only ``approved`` rows produce an ``Approval``; a rejection returns the
        rejection so the caller can distinguish "rejected" from "never decided", which are
        different states and must not both read as None.
        """
        row = self._connection.execute(
            "SELECT * FROM audit_events WHERE finding_id = ? "
            "AND action IN ('approved', 'rejected') ORDER BY audit_id DESC LIMIT 1",
            (str(finding_id),),
        ).fetchone()
        if row is None:
            return None

        return Approval(
            run_id=RunId(row["run_id"]),
            finding_id=FindingId(row["finding_id"]),
            action=ApprovalAction(row["action"]),
            actor=row["actor"],
            payload_hash=row["payload_hash"] or "",
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def close(self) -> None:
        self._connection.close()


def _to_event(row: sqlite3.Row) -> AuditEvent:
    return AuditEvent(
        run_id=RunId(row["run_id"]),
        action=AuditAction(row["action"]),
        actor=row["actor"],
        finding_id=FindingId(row["finding_id"]) if row["finding_id"] else None,
        payload_hash=row["payload_hash"],
        detail=json.loads(row["detail"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )
