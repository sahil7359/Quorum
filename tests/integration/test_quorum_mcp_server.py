"""Quorum's own MCP server, driven by a real MCP client over real stdio.

The mirror of Phase 2: there a real client spoke to a fake GitHub server; here a real client
speaks to *our* server. Same protocol, opposite direction — which means the publishing half of
the MCP skill is exercised, not just described.

The property this file exists to protect: **the MCP surface has no write path.**
"""

from __future__ import annotations

import ast
import inspect
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.infrastructure.mcp import quorum_server as server_module
from app.infrastructure.mcp.allowlist import WRITE_TOOLS
from app.infrastructure.mcp.quorum_server import READ_ONLY_TOOLS, QuorumMcpServer, ReviewRecord
from tests.support.quorum_mcp_stdio import CHUNK_ID

SERVER = str(Path(__file__).resolve().parents[1] / "support" / "quorum_mcp_stdio.py")

pytestmark = pytest.mark.integration


@asynccontextmanager
async def connected() -> AsyncIterator[ClientSession]:
    params = StdioServerParameters(command=sys.executable, args=[SERVER], env=None)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        yield session


def payload(result: Any) -> dict[str, Any]:
    if result.structured_content:
        content = result.structured_content
        if isinstance(content, dict):
            unwrapped = content.get("result", content)
            if isinstance(unwrapped, dict):
                return unwrapped
    decoded: dict[str, Any] = json.loads(result.content[0].text)
    return decoded


class TestPublishedSurface:
    async def test_server_advertises_exactly_the_documented_tools(self) -> None:
        async with connected() as session:
            listing = await session.list_tools()

        assert {tool.name for tool in listing.tools} == set(READ_ONLY_TOOLS)

    async def test_every_tool_has_a_description(self) -> None:
        """A published tool schema is a public interface; an undescribed tool is unusable."""
        async with connected() as session:
            listing = await session.list_tools()

        for tool in listing.tools:
            assert tool.description and len(tool.description.strip()) > 20, tool.name

    async def test_tool_schemas_declare_their_inputs(self) -> None:
        async with connected() as session:
            listing = await session.list_tools()
        schemas = {tool.name: tool.input_schema for tool in listing.tools}

        assert "repo" in schemas["review_pull_request"]["properties"]
        assert "pr_number" in schemas["review_pull_request"]["properties"]
        assert "chunk_id" in schemas["get_chunk"]["properties"]


class TestNoWritePath:
    """The single most important property of this surface."""

    async def test_mcp_server_has_no_write_path(self) -> None:
        async with connected() as session:
            listing = await session.list_tools()
        advertised = {tool.name for tool in listing.tools}

        assert not advertised & WRITE_TOOLS
        assert not any(
            word in name
            for name in advertised
            for word in ("post", "comment", "publish", "approve", "merge", "write", "delete")
        )

    async def test_calling_a_write_tool_by_name_fails(self) -> None:
        """Not merely undocumented — not present."""
        async with connected() as session:
            result = await session.call_tool("add_pull_request_review_comment", {"body": "hello"})
        assert result.is_error

    def test_the_server_module_never_imports_a_code_host(self) -> None:
        """Structural: it has nothing to post *with*, not merely nothing that posts."""
        source = inspect.getsource(server_module)
        tree = ast.parse(source)
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
            for alias in node.names
        }
        assert "GitHubMcpClient" not in imported
        assert "PublishNode" not in imported

    async def test_every_response_states_that_nothing_was_posted(self) -> None:
        """A client integrating against this should not have to read the README."""
        async with connected() as session:
            result = await session.call_tool(
                "review_pull_request", {"repo": "acme/widget", "pr_number": 42}
            )
        body = payload(result)

        assert body["posted_to_github"] is False
        assert "read-only" in body["note"]


class TestReviewPullRequest:
    async def test_returns_findings_with_citations(self) -> None:
        async with connected() as session:
            result = await session.call_tool(
                "review_pull_request", {"repo": "acme/widget", "pr_number": 42}
            )
        body = payload(result)

        assert body["run_id"] == "run-abc"
        assert body["specialists"] == ["correctness", "security"]
        assert body["routing_reason"]
        finding = body["findings"][0]
        assert finding["citation"]["chunk_id"] == CHUNK_ID
        assert finding["citation"]["section_path"] == "Sessions > Expiry"

    async def test_the_routing_reason_is_published_not_just_logged(self) -> None:
        """Routing accuracy is a published metric; a client should see the rationale too."""
        async with connected() as session:
            result = await session.call_tool(
                "review_pull_request", {"repo": "acme/widget", "pr_number": 42}
            )
        assert "app/auth/" in payload(result)["routing_reason"]

    async def test_dropped_findings_are_reported(self) -> None:
        """Cite-or-drop is visible to the caller, not hidden as a silent filter."""
        async with connected() as session:
            result = await session.call_tool(
                "review_pull_request", {"repo": "acme/widget", "pr_number": 42}
            )
        assert payload(result)["dropped"] == ["no_citation"]

    @pytest.mark.parametrize("repo", ["noslash", "a/b/c", "../etc/passwd"])
    async def test_malformed_repo_is_rejected(self, repo: str) -> None:
        async with connected() as session:
            result = await session.call_tool("review_pull_request", {"repo": repo, "pr_number": 1})
        assert "error" in payload(result)

    async def test_non_positive_pr_number_is_rejected(self) -> None:
        async with connected() as session:
            result = await session.call_tool(
                "review_pull_request", {"repo": "acme/widget", "pr_number": 0}
            )
        assert "error" in payload(result)


class TestGetChunk:
    async def test_a_client_can_verify_a_citation(self) -> None:
        """Without this, a chunk id is an opaque token the caller has to trust."""
        async with connected() as session:
            result = await session.call_tool("get_chunk", {"chunk_id": CHUNK_ID})
        body = payload(result)

        assert body["content"] == "All issued tokens must carry an expiry."
        assert body["file_path"] == "docs/security.md"

    async def test_unknown_chunk_is_not_found(self) -> None:
        async with connected() as session:
            result = await session.call_tool("get_chunk", {"chunk_id": "0123456789abcdef"})
        assert payload(result)["error"] == "not_found"

    @pytest.mark.parametrize("bad", ["short", "zzzzzzzzzzzzzzzz", ""])
    async def test_malformed_chunk_id_is_rejected(self, bad: str) -> None:
        async with connected() as session:
            result = await session.call_tool("get_chunk", {"chunk_id": bad})
        assert "malformed" in payload(result)["error"]

    async def test_case_is_normalised(self) -> None:
        async with connected() as session:
            result = await session.call_tool("get_chunk", {"chunk_id": CHUNK_ID.upper()})
        assert "content" in payload(result)


class TestGetReviewAndRepos:
    async def test_unknown_run_id_is_not_found(self) -> None:
        async with connected() as session:
            result = await session.call_tool("get_review", {"run_id": "nope"})
        assert payload(result)["error"] == "not_found"

    async def test_a_review_can_be_fetched_after_it_is_run(self) -> None:
        async with connected() as session:
            await session.call_tool("review_pull_request", {"repo": "acme/widget", "pr_number": 42})
            result = await session.call_tool("get_review", {"run_id": "run-abc"})

        assert payload(result)["run_id"] == "run-abc"

    async def test_ingested_repos_are_listed(self) -> None:
        async with connected() as session:
            result = await session.call_tool("list_ingested_repos", {})
        assert payload(result)["repos"][0]["repo"] == "acme/widget"


def test_review_record_serialisation_is_stable() -> None:
    """The published schema is an interface; changing it silently breaks clients."""
    record = ReviewRecord(
        run_id="r",
        repo="a/b",
        pr_number=1,
        status="proposed",
        specialists=["correctness"],
        routing_reason="because",
        findings=[],
        dropped=[],
    )
    body = server_module._record_to_dict(record)

    assert set(body) == {
        "run_id",
        "repo",
        "pr_number",
        "status",
        "specialists",
        "routing_reason",
        "findings",
        "dropped",
        "diff_truncated",
        "posted_to_github",
        "note",
    }


def test_server_is_constructible_without_any_write_capable_dependency() -> None:
    """The constructor takes three read-only callables. There is nothing to post with."""
    signature = inspect.signature(QuorumMcpServer.__init__)
    assert set(signature.parameters) - {"self"} == {
        "review",
        "resolve_chunk",
        "ingested_repos",
        "reviews",
    }
