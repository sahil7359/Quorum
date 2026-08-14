"""Quorum published as an MCP server.

Consuming an MCP server is now table stakes. *Publishing* one is the rarer half of the skill,
so this module gets the same care as the client.

**The single most important property: this surface has no write path.**

An MCP client calling Quorum gets findings, not side effects. If `review_pull_request` could
post to GitHub, any MCP client could bypass the human approval gate that the entire project
exists to demonstrate — and it would bypass it through the door I opened for convenience.

That is not enforced by discipline. The server is constructed with a graph built as
``build_review_graph(..., approval=None, publish=None)``, so the object it holds **physically
has no publish node**. There is nothing to call. ``test_mcp_server_has_no_write_path`` asserts
both the absence of write tools and the absence of the node.

Tools published:

| Tool | Purpose |
| --- | --- |
| ``review_pull_request`` | Run a review, return findings with citations. Never posts. |
| ``get_review`` | Fetch a previously computed review by run id. |
| ``list_ingested_repos`` | Which repositories are available to review against. |
| ``get_chunk`` | Resolve a chunk id to its text and ``(file, section, offset)`` locator. |

``get_chunk`` exists so a *client* can verify a citation. Without it, a finding's chunk id is
an opaque token the caller has to trust; with it, grounding is checkable by whoever consumes
the review rather than only by someone with our database.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from mcp.server.mcpserver import MCPServer

from app.domain.entities import Finding, RepoRef
from app.domain.values import ChunkId, RunId

SERVER_NAME = "quorum"
SERVER_VERSION = "0.1.0"

READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {"review_pull_request", "get_review", "list_ingested_repos", "get_chunk"}
)
"""Every tool this server publishes. All read-only, by construction and by test."""


def finding_to_dict(finding: Finding) -> dict[str, Any]:
    """Serialise a finding with its citation.

    The citation is always present because a ``Finding`` cannot exist without one -- the type
    guarantees it, so this function has no ``if citation is not None`` branch and never will.
    """
    locator = finding.citation.locator
    return {
        "finding_id": str(finding.finding_id),
        "specialist": finding.specialist.value,
        "severity": finding.severity.value,
        "confidence": finding.confidence,
        "title": finding.title,
        "body": finding.body,
        "file_path": finding.file_path,
        "line_start": finding.line_start,
        "citation": {
            "chunk_id": str(finding.citation.chunk_id),
            "file_path": locator.file_path,
            "section_path": locator.section_path,
            "start_line": None,
            "byte_range": [locator.start_offset, locator.end_offset],
            "display": finding.citation.display,
        },
    }


@dataclass
class ReviewRecord:
    """One completed review, as the server remembers it."""

    run_id: str
    repo: str
    pr_number: int
    status: str
    specialists: list[str]
    routing_reason: str
    findings: list[dict[str, Any]]
    dropped: list[str]
    diff_truncated: bool = False


@dataclass
class QuorumMcpServer:
    """The MCP surface.

    Takes a ``review`` callable rather than a graph so that the transport layer is testable
    without a model, a retriever or a code host. The composition root supplies the real one.
    """

    review: Callable[[RepoRef, int], Awaitable[ReviewRecord]]
    resolve_chunk: Callable[[ChunkId], Awaitable[dict[str, Any] | None]]
    ingested_repos: Callable[[], list[dict[str, Any]]]
    reviews: dict[str, ReviewRecord] = field(default_factory=dict)

    def build(self) -> MCPServer:
        server = MCPServer(name=SERVER_NAME, version=SERVER_VERSION)

        @server.tool()
        async def review_pull_request(repo: str, pr_number: int) -> dict[str, Any]:
            """Review a pull request and return grounded findings.

            This tool NEVER posts to GitHub. Findings are returned for a human to act on;
            publishing requires approval through Quorum's own interface.
            """
            try:
                reference = RepoRef.parse(repo)
            except ValueError as exc:
                return {"error": f"invalid repo: {exc}", "findings": []}

            if pr_number <= 0:
                return {"error": "pr_number must be positive", "findings": []}

            record = await self.review(reference, pr_number)
            self.reviews[record.run_id] = record
            return _record_to_dict(record)

        @server.tool()
        async def get_review(run_id: str) -> dict[str, Any]:
            """Fetch a previously computed review by run id."""
            record = self.reviews.get(run_id)
            if record is None:
                return {"error": "not_found", "run_id": run_id}
            return _record_to_dict(record)

        @server.tool()
        async def list_ingested_repos() -> dict[str, Any]:
            """Repositories whose documentation has been ingested and can be reviewed against."""
            return {"repos": self.ingested_repos()}

        @server.tool()
        async def get_chunk(chunk_id: str) -> dict[str, Any]:
            """Resolve a chunk id to its text and locator, so a client can verify a citation."""
            try:
                identifier = ChunkId(chunk_id.strip().lower())
            except ValueError as exc:
                return {"error": f"malformed chunk_id: {exc}"}

            resolved = await self.resolve_chunk(identifier)
            if resolved is None:
                return {"error": "not_found", "chunk_id": chunk_id}
            return resolved

        return server


def _record_to_dict(record: ReviewRecord) -> dict[str, Any]:
    return {
        "run_id": record.run_id,
        "repo": record.repo,
        "pr_number": record.pr_number,
        "status": record.status,
        "specialists": record.specialists,
        "routing_reason": record.routing_reason,
        "findings": record.findings,
        "dropped": record.dropped,
        "diff_truncated": record.diff_truncated,
        # Stated in every response rather than only in documentation, because a client
        # integrating against this should not have to read the README to learn that Quorum
        # did not touch their repository.
        "posted_to_github": False,
        "note": (
            "Quorum's MCP surface is read-only. Publishing a finding requires human approval "
            "through Quorum's own interface and is not reachable from this server."
        ),
    }


def run_id_of(record: ReviewRecord) -> RunId:
    return RunId(record.run_id)
