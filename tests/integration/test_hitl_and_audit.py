"""Human-in-the-loop approval, durable resumption, and the append-only audit trail.

The two claims this project exists to make are tested here:

* **Nothing reaches GitHub without a human approving that exact text.**
* **A review proposed at 14:00 and approved at 19:00 resumes in a different process.**

The second one needs a real checkpointer and a real process boundary, so the resumption test
builds a *fresh* graph and a *fresh* saver against the same SQLite file — which is as close to
"the instance slept and woke up" as a test can get without actually killing a process.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from app.application.agents.approval import (
    ApprovalNode,
    PublishNode,
    assert_publishable,
    decisions_from_payload,
)
from app.application.agents.graph import build_review_graph
from app.application.agents.nodes import IngestNode, RouteNode, SpecialistsNode, SynthesiseNode
from app.domain.entities import (
    Approval,
    AuditEvent,
    ChangedFile,
    Chunk,
    Citation,
    Diff,
    DiffHunk,
    Finding,
    PullRequest,
    RepoRef,
    ScoredChunk,
)
from app.domain.errors import ApprovalRequiredError
from app.domain.values import (
    ApprovalAction,
    AuditAction,
    ChunkId,
    ChunkLocator,
    FindingId,
    RunId,
    Severity,
    SpecialistKind,
)
from app.infrastructure.persistence.audit import SqliteAuditLog
from tests.support.fakes import (
    FakeChatModel,
    FakeCodeHost,
    FakeRetriever,
    FrozenClock,
    NullTracer,
    RecordingLogger,
)

REPO = RepoRef.parse("acme/widget")

LOCATOR = ChunkLocator(
    repo="acme/widget",
    commit_sha="head5678",
    file_path="docs/security.md",
    section_path="Sessions > Expiry",
    start_offset=0,
    end_offset=200,
)
CHUNK = Chunk.create(
    locator=LOCATOR,
    content="All issued tokens must carry an expiry.",
    start_line=1,
    end_line=3,
    ordinal=0,
    token_count=20,
)


def a_finding(title: str = "Token never expires") -> Finding:
    return Finding(
        finding_id=FindingId.new(),
        specialist=SpecialistKind.SECURITY,
        severity=Severity.HIGH,
        confidence=0.9,
        title=title,
        body="Tokens must carry an expiry.",
        citation=Citation(chunk_id=ChunkId.derive(LOCATOR), locator=LOCATOR),
        file_path="app/auth/login.py",
        line_start=12,
    )


class TestAuditIsAppendOnly:
    """Guardrail T6. Enforced by the database, not by the absence of a method."""

    async def test_events_are_recorded_and_ordered(self) -> None:
        audit = SqliteAuditLog()
        run = RunId.new()
        finding = a_finding()

        for action in (AuditAction.PROPOSED, AuditAction.APPROVED, AuditAction.POSTED):
            await audit.append(
                AuditEvent(
                    run_id=run,
                    action=action,
                    actor="sahil",
                    finding_id=finding.finding_id,
                    payload_hash=finding.payload_hash,
                )
            )

        history = await audit.history(run)
        assert [e.action for e in history] == [
            AuditAction.PROPOSED,
            AuditAction.APPROVED,
            AuditAction.POSTED,
        ]

    async def test_update_is_refused_by_the_database(self) -> None:
        """Someone with a connection and good intentions still cannot rewrite history."""
        audit = SqliteAuditLog()
        await audit.append(AuditEvent(run_id=RunId.new(), action=AuditAction.POSTED, actor="sahil"))

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            audit._connection.execute("UPDATE audit_events SET actor = 'someone else'")

    async def test_delete_is_refused_by_the_database(self) -> None:
        audit = SqliteAuditLog()
        await audit.append(AuditEvent(run_id=RunId.new(), action=AuditAction.POSTED, actor="sahil"))

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            audit._connection.execute("DELETE FROM audit_events")

    async def test_the_adapter_exposes_no_mutation_method(self) -> None:
        """The application has no vocabulary for updating an audit row."""
        surface = {name for name in dir(SqliteAuditLog) if not name.startswith("_")}
        assert not surface & {"update", "delete", "remove", "purge", "clear"}

    async def test_a_rejection_is_distinguishable_from_no_decision(self) -> None:
        """Different states that must not both read as ``None``."""
        audit = SqliteAuditLog()
        finding = a_finding()
        assert await audit.approval_for(finding.finding_id) is None

        await audit.append(
            AuditEvent(
                run_id=RunId.new(),
                action=AuditAction.REJECTED,
                actor="sahil",
                finding_id=finding.finding_id,
                payload_hash=finding.payload_hash,
            )
        )
        decision = await audit.approval_for(finding.finding_id)
        assert decision is not None
        assert decision.action is ApprovalAction.REJECTED
        assert not decision.authorises(finding)

    async def test_the_latest_decision_wins(self) -> None:
        """A re-approval after an edit must override the earlier rejection."""
        audit = SqliteAuditLog()
        run = RunId.new()
        finding = a_finding()
        for action in (AuditAction.REJECTED, AuditAction.APPROVED):
            await audit.append(
                AuditEvent(
                    run_id=run,
                    action=action,
                    actor="sahil",
                    finding_id=finding.finding_id,
                    payload_hash=finding.payload_hash,
                )
            )

        decision = await audit.approval_for(finding.finding_id)
        assert decision is not None
        assert decision.action is ApprovalAction.APPROVED


class TestPublishGuard:
    """Guardrails G4 and G5, checked against the audit log rather than against state."""

    async def _publish(self, audit: SqliteAuditLog, finding: Finding, run: RunId) -> Any:
        host = FakeCodeHost()
        node = PublishNode(
            code_host=host,
            audit=audit,
            clock=FrozenClock(),
            logger=RecordingLogger(),
            tracer=NullTracer(),
        )
        return await node.run({"run_id": run, "repo": REPO, "pr_number": 42, "findings": [finding]})

    async def test_publish_requires_an_approval_row(self) -> None:
        audit = SqliteAuditLog()
        result = await self._publish(audit, a_finding(), RunId.new())

        assert result["posted"] == []
        assert result["refused"]

    async def test_a_rejection_does_not_authorise_a_post(self) -> None:
        audit = SqliteAuditLog()
        run, finding = RunId.new(), a_finding()
        await audit.append(
            AuditEvent(
                run_id=run,
                action=AuditAction.REJECTED,
                actor="sahil",
                finding_id=finding.finding_id,
                payload_hash=finding.payload_hash,
            )
        )

        assert (await self._publish(audit, finding, run))["posted"] == []

    async def test_edited_finding_requires_reapproval(self) -> None:
        """The approval is bound to the exact text the human read."""
        audit = SqliteAuditLog()
        run, original = RunId.new(), a_finding("Token never expires")
        await audit.append(
            AuditEvent(
                run_id=run,
                action=AuditAction.APPROVED,
                actor="sahil",
                finding_id=original.finding_id,
                payload_hash=original.payload_hash,
            )
        )

        edited = Finding(
            finding_id=original.finding_id,
            specialist=original.specialist,
            severity=original.severity,
            confidence=original.confidence,
            title="Something the reviewer never saw",
            body=original.body,
            citation=original.citation,
        )

        assert (await self._publish(audit, edited, run))["posted"] == []

    async def test_a_refusal_is_itself_audited(self) -> None:
        """The record has to show what was refused, not only what was posted."""
        audit = SqliteAuditLog()
        run = RunId.new()
        await self._publish(audit, a_finding(), run)

        assert any(e.action is AuditAction.REFUSED for e in await audit.history(run))

    async def test_standalone_guard_raises_for_callers_outside_the_graph(self) -> None:
        """The API and the MCP server get the same guard, and it stops them."""
        audit = SqliteAuditLog()
        with pytest.raises(ApprovalRequiredError):
            await assert_publishable(audit, a_finding(), RunId.new())

    async def test_standalone_guard_rejects_an_approval_from_another_run(self) -> None:
        audit = SqliteAuditLog()
        finding = a_finding()
        await audit.append(
            AuditEvent(
                run_id=RunId.new(),
                action=AuditAction.APPROVED,
                actor="sahil",
                finding_id=finding.finding_id,
                payload_hash=finding.payload_hash,
            )
        )
        with pytest.raises(ApprovalRequiredError, match="different run"):
            await assert_publishable(audit, finding, RunId.new())


class TestDecisionParsing:
    @pytest.mark.parametrize(
        "payload",
        [
            "not a list",
            None,
            [{"action": "approved"}],
            [{"finding_id": "x"}],
            [{"finding_id": "x", "action": "maybe"}],
            [42],
        ],
    )
    def test_malformed_decisions_are_discarded(self, payload: object) -> None:
        """A malformed decision must never become an approval."""
        assert decisions_from_payload(payload) == []

    def test_valid_decision_is_parsed(self) -> None:
        parsed = decisions_from_payload(
            [{"finding_id": "abc", "action": "approved", "actor": "sahil", "note": "ok"}]
        )
        assert len(parsed) == 1
        assert parsed[0].action is ApprovalAction.APPROVED


class TestDurableInterrupt:
    """The claim: a review can be approved hours later, in a different process."""

    def _graph(self, saver: Any, audit: SqliteAuditLog, host: FakeCodeHost) -> Any:
        logger, tracer = RecordingLogger(), NullTracer()
        model = FakeChatModel(
            responses={
                "route": ['{"specialists": ["correctness"], "reason": "x"}'],
                "security": [
                    '{"findings": [{"title": "Token never expires", "body": "Tokens must '
                    'carry an expiry.", "severity": "high", "confidence": 0.9, "chunk_id": "'
                    + str(CHUNK.chunk_id)
                    + '"}]}'
                ],
            }
        )
        return build_review_graph(
            ingest=IngestNode(
                code_host=host,
                logger=logger,
                tracer=tracer,
                max_diff_lines=1500,
            ),
            route=RouteNode(model=model, logger=logger, tracer=tracer),
            specialists=SpecialistsNode(
                retriever=FakeRetriever(chunks=[ScoredChunk(chunk=CHUNK, score=0.9)]),
                model=model,
                logger=logger,
                tracer=tracer,
                top_k=5,
            ),
            synthesise=SynthesiseNode(logger=logger, tracer=tracer),
            approval=ApprovalNode(audit=audit, clock=FrozenClock(), logger=logger, tracer=tracer),
            publish=PublishNode(
                code_host=host,
                audit=audit,
                clock=FrozenClock(),
                logger=logger,
                tracer=tracer,
            ),
            checkpointer=saver,
        )

    def _host(self) -> FakeCodeHost:
        hunk = DiffHunk("app/auth/login.py", 1, 3, 1, 3, "+    token.expires_at = None")
        return FakeCodeHost(
            pull_request=PullRequest(
                repo=REPO,
                number=42,
                title="Issue tokens",
                body="",
                author="octocat",
                base_sha="base",
                head_sha="head5678",
            ),
            diff=Diff(
                files=(ChangedFile("app/auth/login.py", "modified", 1, 0, (hunk,)),),
                raw="diff --git a/app/auth/login.py b/app/auth/login.py\n",
            ),
            files={
                "app/auth/login.py": "def issue():\n    token.expires_at = None\n    return t\n"
            },
        )

    async def test_graph_stops_at_the_gate_and_posts_nothing(self, tmp_path: Path) -> None:
        audit = SqliteAuditLog()
        host = self._host()
        config = {"configurable": {"thread_id": "run-1"}}

        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite")) as saver:
            graph = self._graph(saver, audit, host)
            result = await graph.ainvoke(
                {"run_id": RunId.new(), "repo": REPO, "pr_number": 42}, config
            )

        assert "__interrupt__" in result
        assert host.posted == []

    async def test_a_review_resumes_in_a_fresh_process_and_publishes(self, tmp_path: Path) -> None:
        """The load-bearing test.

        Two separate savers over the same file, two separate graph objects — the second one
        knows nothing except what was checkpointed. This is the free-tier instance sleeping
        and waking up.
        """
        database = str(tmp_path / "cp.sqlite")
        audit = SqliteAuditLog(tmp_path / "audit.sqlite")
        host = self._host()
        config = {"configurable": {"thread_id": "run-2"}}

        async with AsyncSqliteSaver.from_conn_string(database) as saver:
            proposed = await self._graph(saver, audit, host).ainvoke(
                {"run_id": RunId.new(), "repo": REPO, "pr_number": 42}, config
            )
        interrupt_payload = proposed["__interrupt__"][0].value
        finding_id = interrupt_payload["findings"][0]["finding_id"]
        assert host.posted == []

        # --- process boundary: everything above is gone except the checkpoint ---

        async with AsyncSqliteSaver.from_conn_string(database) as saver:
            resumed = await self._graph(saver, audit, host).ainvoke(
                Command(
                    resume=[{"finding_id": finding_id, "action": "approved", "actor": "sahil"}]
                ),
                config,
            )

        assert resumed["posted"], "an approved finding should have been posted after resume"
        assert len(host.posted) == 1

    async def test_rejecting_after_a_restart_posts_nothing(self, tmp_path: Path) -> None:
        database = str(tmp_path / "cp.sqlite")
        audit = SqliteAuditLog(tmp_path / "audit.sqlite")
        host = self._host()
        config = {"configurable": {"thread_id": "run-3"}}

        async with AsyncSqliteSaver.from_conn_string(database) as saver:
            proposed = await self._graph(saver, audit, host).ainvoke(
                {"run_id": RunId.new(), "repo": REPO, "pr_number": 42}, config
            )
        finding_id = proposed["__interrupt__"][0].value["findings"][0]["finding_id"]

        async with AsyncSqliteSaver.from_conn_string(database) as saver:
            resumed = await self._graph(saver, audit, host).ainvoke(
                Command(
                    resume=[{"finding_id": finding_id, "action": "rejected", "actor": "sahil"}]
                ),
                config,
            )

        assert resumed["posted"] == []
        assert host.posted == []

    async def test_never_responding_posts_nothing(self, tmp_path: Path) -> None:
        """Correct behaviour, not a bug: an undecided review stays proposed forever."""
        audit = SqliteAuditLog()
        host = self._host()

        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite")) as saver:
            await self._graph(saver, audit, host).ainvoke(
                {"run_id": RunId.new(), "repo": REPO, "pr_number": 42},
                {"configurable": {"thread_id": "run-4"}},
            )

        assert host.posted == []


def test_approval_authorises_only_its_own_finding() -> None:
    finding, other = a_finding("A"), a_finding("B")
    approval = Approval(
        run_id=RunId.new(),
        finding_id=finding.finding_id,
        action=ApprovalAction.APPROVED,
        actor="sahil",
        payload_hash=finding.payload_hash,
    )
    assert approval.authorises(finding)
    assert not approval.authorises(other)


class TestPublishDoesNotTrustState:
    """The audit log is the record; graph state is a courier that crossed a checkpoint.

    Added because a gate proof failed to fail: swapping the audit lookup for a state lookup
    left every other publish test green. They call the node with no ``approvals`` in state, so
    both implementations refused for the same reason and the distinction was untested.

    A forged approval in state with nothing in the audit log is exactly the shape of the bug
    that design decision exists to prevent.
    """

    async def test_an_approval_present_only_in_state_does_not_authorise_a_post(self) -> None:
        audit = SqliteAuditLog()  # deliberately empty
        finding = a_finding()
        forged = Approval(
            run_id=RunId.new(),
            finding_id=finding.finding_id,
            action=ApprovalAction.APPROVED,
            actor="attacker",
            payload_hash=finding.payload_hash,
        )
        host = FakeCodeHost()
        node = PublishNode(
            code_host=host,
            audit=audit,
            clock=FrozenClock(),
            logger=RecordingLogger(),
            tracer=NullTracer(),
        )

        result = await node.run(
            {
                "run_id": RunId.new(),
                "repo": REPO,
                "pr_number": 42,
                "findings": [finding],
                "approvals": [forged],
            }
        )

        assert result["posted"] == []
        assert host.posted == []
        assert result["refused"] == [str(finding.finding_id)]
