"""Launch Quorum's MCP server over stdio with a stubbed review function.

Used by the integration test so a *real* MCP client can speak *real* MCP to *our* server —
the mirror image of the Phase 2 setup, where a real client spoke to a fake GitHub server.
Here the server is genuine and only the review computation behind it is stubbed.

Run: ``python -m tests.support.quorum_mcp_stdio``
"""

from __future__ import annotations

from typing import Any

from app.domain.entities import RepoRef
from app.domain.values import ChunkId
from app.infrastructure.mcp.quorum_server import QuorumMcpServer, ReviewRecord

CHUNK_ID = "5a5980876bf3a070"

STUB_FINDING: dict[str, Any] = {
    "finding_id": "11111111-1111-1111-1111-111111111111",
    "specialist": "security",
    "severity": "high",
    "confidence": 0.9,
    "title": "Token never expires",
    "body": "Tokens must carry an expiry.",
    "file_path": "app/auth/login.py",
    "line_start": 12,
    "citation": {
        "chunk_id": CHUNK_ID,
        "file_path": "docs/security.md",
        "section_path": "Sessions > Expiry",
        "start_line": None,
        "byte_range": [0, 200],
        "display": "docs/security.md — Sessions > Expiry",
    },
}


async def _review(repo: RepoRef, pr_number: int) -> ReviewRecord:
    return ReviewRecord(
        run_id="run-abc",
        repo=str(repo),
        pr_number=pr_number,
        status="proposed",
        specialists=["correctness", "security"],
        routing_reason="diff touches app/auth/",
        findings=[STUB_FINDING],
        dropped=["no_citation"],
    )


async def _resolve_chunk(chunk_id: ChunkId) -> dict[str, Any] | None:
    if str(chunk_id) != CHUNK_ID:
        return None
    return {
        "chunk_id": CHUNK_ID,
        "content": "All issued tokens must carry an expiry.",
        "file_path": "docs/security.md",
        "section_path": "Sessions > Expiry",
        "byte_range": [0, 200],
    }


def _repos() -> list[dict[str, Any]]:
    return [{"repo": "acme/widget", "ingested_sha": "head5678", "chunks": 164}]


if __name__ == "__main__":
    QuorumMcpServer(
        review=_review, resolve_chunk=_resolve_chunk, ingested_repos=_repos
    ).build().run(transport="stdio")
