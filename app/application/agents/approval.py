"""The human approval gate, and the publish guard behind it.

The graph stops at ``interrupt()``. State is checkpointed, so **the process may die here** —
a free-tier instance sleeps, and a review proposed at 14:00 and approved at 19:00 must resume
in a different process with the same state. That is the whole reason the checkpointer is
durable rather than in-memory.

The publish guard does not trust the graph. It re-reads the audit log and asks the *approval*
whether it authorises this exact finding, matching both identity and ``payload_hash``. Graph
topology is a convention; a guard is not.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from langgraph.types import interrupt

from app.application.agents.nodes import TracedNode
from app.application.agents.state import ReviewState
from app.domain import log_events
from app.domain.entities import Approval, AuditEvent, Finding
from app.domain.errors import ApprovalRequiredError
from app.domain.ports import AuditPort, ClockPort, CodeHostPort, LoggerPort, TracerPort
from app.domain.values import ApprovalAction, AuditAction, FindingId, RunId, RunStatus


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """One human decision, as it arrives back through ``Command(resume=...)``."""

    finding_id: str
    action: ApprovalAction
    actor: str
    note: str = ""


def decisions_from_payload(payload: object) -> list[ApprovalDecision]:
    """Parse the resume payload. Anything unrecognised is discarded, not guessed at.

    A malformed decision must not become an approval. Silence is the safe default here:
    an undecided finding stays proposed forever and nothing is posted, which is correct.
    """
    if not isinstance(payload, list):
        return []

    decisions: list[ApprovalDecision] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            action = ApprovalAction(str(item.get("action", "")).strip().lower())
        except ValueError:
            continue
        finding_id = item.get("finding_id")
        if not isinstance(finding_id, str) or not finding_id:
            continue
        decisions.append(
            ApprovalDecision(
                finding_id=finding_id,
                action=action,
                actor=str(item.get("actor", "unknown")),
                note=str(item.get("note", "")),
            )
        )
    return decisions


class ApprovalNode(TracedNode):
    """Propose findings to a human and block until they decide."""

    name = "approval"

    def __init__(
        self,
        *,
        audit: AuditPort,
        clock: ClockPort,
        logger: LoggerPort,
        tracer: TracerPort,
    ) -> None:
        super().__init__(logger=logger, tracer=tracer)
        self._audit = audit
        self._clock = clock

    async def run(self, state: ReviewState) -> dict[str, object]:
        findings: Sequence[Finding] = state.get("findings", [])
        run_id = state["run_id"]

        if not findings:
            # Nothing to approve is a clean outcome, not an interrupt. Stopping to ask a human
            # about an empty list would be the fastest way to make the gate feel like noise.
            return {"approvals": [], "status": RunStatus.PUBLISHED}

        for finding in findings:
            await self._audit.append(
                AuditEvent(
                    run_id=run_id,
                    action=AuditAction.PROPOSED,
                    actor="system",
                    finding_id=finding.finding_id,
                    payload_hash=finding.payload_hash,
                    detail={"title": finding.title, "specialist": finding.specialist.value},
                    created_at=self._clock.now(),
                )
            )
        self._safe_log("info", log_events.APPROVAL_PROPOSED, findings=len(findings))

        # Durable. Everything above this line is already persisted; the process may now die.
        payload = interrupt(
            {
                "run_id": str(run_id),
                "findings": [
                    {
                        "finding_id": str(f.finding_id),
                        "title": f.title,
                        "body": f.body,
                        "severity": f.severity.value,
                        "specialist": f.specialist.value,
                        "payload_hash": f.payload_hash,
                        "citation": {
                            "chunk_id": str(f.citation.chunk_id),
                            "display": f.citation.display,
                        },
                    }
                    for f in findings
                ],
            }
        )

        by_id = {str(f.finding_id): f for f in findings}
        approvals: list[Approval] = []

        for decision in decisions_from_payload(payload):
            matched = by_id.get(decision.finding_id)
            if matched is None:
                # A decision naming a finding this run never produced is discarded.
                self._safe_log(
                    "warn",
                    log_events.APPROVAL_DECIDED,
                    finding_id=decision.finding_id,
                    action="discarded_unknown_finding",
                    actor=decision.actor,
                )
                continue

            approval = Approval(
                run_id=run_id,
                finding_id=matched.finding_id,
                action=decision.action,
                actor=decision.actor,
                # Bound to the text the human actually saw. If the finding changed after they
                # looked at it, this hash no longer matches and publish refuses it.
                payload_hash=matched.payload_hash,
                note=decision.note,
                created_at=self._clock.now(),
            )
            approvals.append(approval)

            await self._audit.append(
                AuditEvent(
                    run_id=run_id,
                    action=AuditAction(decision.action.value),
                    actor=decision.actor,
                    finding_id=matched.finding_id,
                    payload_hash=matched.payload_hash,
                    detail={"note": decision.note},
                    created_at=self._clock.now(),
                )
            )
            self._safe_log(
                "info",
                log_events.APPROVAL_DECIDED,
                finding_id=str(matched.finding_id),
                action=decision.action.value,
                actor=decision.actor,
            )

        return {"approvals": approvals, "status": RunStatus.PROPOSED}


class PublishNode(TracedNode):
    """The only write path. Every finding is re-checked against the audit log."""

    name = "publish"

    def __init__(
        self,
        *,
        code_host: CodeHostPort,
        audit: AuditPort,
        clock: ClockPort,
        logger: LoggerPort,
        tracer: TracerPort,
    ) -> None:
        super().__init__(logger=logger, tracer=tracer)
        self._code_host = code_host
        self._audit = audit
        self._clock = clock

    async def run(self, state: ReviewState) -> dict[str, object]:
        findings: Sequence[Finding] = state.get("findings", [])
        posted: list[str] = []
        refused: list[str] = []

        for finding in findings:
            # why: re-read from the audit log rather than trusting state["approvals"]. State
            #      travelled through a checkpoint and a resume; the audit table is the record
            #      of what a human actually decided, and it is the thing that must be right.
            #      alt: trust the in-memory approvals list (one bug from an unapproved post)
            approval = await self._audit.approval_for(finding.finding_id)

            if approval is None or not approval.authorises(finding):
                reason = "no_approval" if approval is None else "approval_does_not_authorise"
                refused.append(str(finding.finding_id))
                await self._audit.append(
                    AuditEvent(
                        run_id=state["run_id"],
                        action=AuditAction.REFUSED,
                        actor="system",
                        finding_id=finding.finding_id,
                        payload_hash=finding.payload_hash,
                        detail={"reason": reason},
                        created_at=self._clock.now(),
                    )
                )
                self._safe_log(
                    "error",
                    log_events.PUBLISH_REFUSED,
                    finding_id=str(finding.finding_id),
                    reason=reason,
                )
                continue

            comment_id = await self._code_host.post_review_comment(
                state["repo"], state["pr_number"], finding, approval=approval
            )
            posted.append(comment_id)
            await self._audit.append(
                AuditEvent(
                    run_id=state["run_id"],
                    action=AuditAction.POSTED,
                    actor=approval.actor,
                    finding_id=finding.finding_id,
                    payload_hash=finding.payload_hash,
                    detail={"comment_id": comment_id},
                    created_at=self._clock.now(),
                )
            )

        return {"posted": posted, "refused": refused, "status": RunStatus.PUBLISHED}


async def assert_publishable(audit: AuditPort, finding: Finding, run_id: RunId) -> Approval:
    """Standalone guard for callers outside the graph (the API, the MCP server).

    Raises rather than returning None, because every caller of this must stop.
    """
    approval = await audit.approval_for(finding.finding_id)
    if approval is None:
        raise ApprovalRequiredError(f"finding {finding.finding_id} has no approval")
    if not approval.authorises(finding):
        raise ApprovalRequiredError(
            f"approval for {finding.finding_id} does not authorise this text"
        )
    if approval.run_id != run_id:
        raise ApprovalRequiredError("approval belongs to a different run")
    return approval


def find_id(value: str) -> FindingId:
    return FindingId(value)
