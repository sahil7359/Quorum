"""The MCP client against a real MCP server over real stdio.

No GitHub, no token, no network -- but a genuine subprocess, a genuine JSON-RPC handshake,
genuine tool discovery and genuine structured results. The things I could actually get wrong
are exercised.

Note on structure: each test opens its own ``async with client()`` rather than sharing a
fixture. An async-generator fixture finalises in a different task from the one that entered
it, and the MCP session is backed by an anyio task group, which refuses to be exited from a
foreign task ("Attempted to exit cancel scope in a different task"). Entering and exiting in
the same task is the fix, and it costs one line per test.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.domain.entities import Approval, Citation, Finding, RepoRef
from app.domain.errors import ApprovalRequiredError, CodeHostError, ToolNotAllowedError
from app.domain.values import (
    ApprovalAction,
    ChunkId,
    ChunkLocator,
    FindingId,
    RunId,
    Severity,
    SpecialistKind,
)
from app.infrastructure.mcp.github_client import GitHubMcpClient, render_finding
from tests.support.fakes import RecordingLogger

SERVER = str(Path(__file__).resolve().parents[1] / "support" / "fake_github_mcp_server.py")
REPO = RepoRef.parse("acme/widget")

pytestmark = pytest.mark.integration


def client(
    *,
    mode: str = "normal",
    token: str = "ghp_faketokenfortests000000000000000",
    logger: RecordingLogger | None = None,
) -> GitHubMcpClient:
    return GitHubMcpClient(
        command=sys.executable,
        args=[SERVER],
        token=token,
        logger=logger or RecordingLogger(),
        env={"FAKE_MCP_MODE": mode},
    )


def a_finding(title: str = "Token never expires") -> Finding:
    locator = ChunkLocator(
        repo="acme/widget",
        commit_sha="head5678",
        file_path="docs/security.md",
        section_path="Sessions > Expiry",
        start_offset=0,
        end_offset=200,
    )
    return Finding(
        finding_id=FindingId.new(),
        specialist=SpecialistKind.SECURITY,
        severity=Severity.HIGH,
        confidence=0.9,
        title=title,
        body="Tokens must carry an expiry.",
        citation=Citation(chunk_id=ChunkId.derive(locator), locator=locator),
        file_path="app/auth/login.py",
        line_start=12,
    )


def an_approval(finding: Finding, action: ApprovalAction) -> Approval:
    return Approval(
        run_id=RunId.new(),
        finding_id=finding.finding_id,
        action=action,
        actor="sahil",
        payload_hash=finding.payload_hash,
    )


class TestReads:
    async def test_get_pull_request(self) -> None:
        async with client() as c:
            pr = await c.get_pull_request(REPO, 42)

        assert pr.number == 42
        assert pr.head_sha == "head5678"
        assert pr.author == "octocat"

    async def test_get_diff_parses_into_entities(self) -> None:
        async with client() as c:
            diff = await c.get_diff(REPO, 42, max_lines=1000)

        assert diff.touched_paths == ("app/auth/login.py", "README.md")
        assert not diff.truncated
        assert diff.has_source_changes
        assert not diff.has_test_changes

    async def test_additions_exclude_the_file_header(self) -> None:
        """``+++ b/path`` is a header, not an added line. Counting it skews routing."""
        async with client() as c:
            diff = await c.get_diff(REPO, 42, max_lines=1000)

        login = next(f for f in diff.files if f.file_path == "app/auth/login.py")
        assert login.additions == 3
        assert login.deletions == 1

    async def test_oversize_diff_is_truncated_and_flagged(self) -> None:
        """Guardrail G8. Truncation must be visible -- silence here is a lie about coverage."""
        logger = RecordingLogger()
        async with client(logger=logger) as c:
            diff = await c.get_diff(REPO, 42, max_lines=5)

        assert diff.truncated
        assert diff.truncated_at_line == 5
        assert logger.find("diff.truncated") is not None

    async def test_get_file(self) -> None:
        async with client() as c:
            contents = await c.get_file(REPO, "README.md", ref="head5678")

        assert "README.md" in contents

    async def test_list_changed_files(self) -> None:
        async with client() as c:
            files = await c.list_changed_files(REPO, 42)

        assert [f.file_path for f in files] == ["app/auth/login.py", "README.md"]


class TestAllowlist:
    async def test_non_allowlisted_tool_is_refused(self) -> None:
        """``whoami`` exists on the server and is refused anyway. Guardrail G3."""
        async with client() as c:
            with pytest.raises(ToolNotAllowedError, match="whoami"):
                await c._call("whoami", {})

    async def test_destructive_tool_is_refused_even_when_advertised(self) -> None:
        """The server offers ``delete_repository``. The client will not reach for it."""
        async with client(mode="extra") as c:
            with pytest.raises(ToolNotAllowedError, match="delete_repository"):
                await c._call("delete_repository", {"owner": "acme", "repo": "widget"})

    async def test_unvetted_tools_are_logged_not_ignored(self) -> None:
        """A server that grows a capability should be visible, not silently tolerated."""
        logger = RecordingLogger()
        async with client(mode="extra", logger=logger):
            pass

        line = logger.find("mcp.tools.unvetted_available")
        assert line is not None
        assert "delete_repository" in line.fields["sample"]  # type: ignore[operator]

    async def test_missing_read_tool_refuses_to_connect(self) -> None:
        """A review that silently skipped the diff would look like a clean review."""
        with pytest.raises(CodeHostError, match="get_pull_request_diff"):
            async with client(mode="missing"):
                pass


class TestWriteGuard:
    async def test_approved_finding_is_posted(self) -> None:
        finding = a_finding()
        approval = an_approval(finding, ApprovalAction.APPROVED)

        async with client() as c:
            comment_id = await c.post_review_comment(REPO, 42, finding, approval=approval)

        assert comment_id.startswith("comment-")

    async def test_publish_requires_approval_row(self) -> None:
        """Guardrail G4. A rejection is not an approval."""
        finding = a_finding()
        rejection = an_approval(finding, ApprovalAction.REJECTED)

        async with client() as c:
            with pytest.raises(ApprovalRequiredError):
                await c.post_review_comment(REPO, 42, finding, approval=rejection)

    async def test_edited_finding_requires_reapproval(self) -> None:
        """Guardrail G5: approval is bound to exact text via ``payload_hash``."""
        original = a_finding("Token never expires")
        approval = an_approval(original, ApprovalAction.APPROVED)
        edited = a_finding("Something the reviewer never saw")

        async with client() as c:
            with pytest.raises(ApprovalRequiredError):
                await c.post_review_comment(REPO, 42, edited, approval=approval)

    async def test_approval_for_a_different_finding_does_not_transfer(self) -> None:
        approved = a_finding("Approved one")
        approval = an_approval(approved, ApprovalAction.APPROVED)
        other = a_finding("A different finding entirely")

        async with client() as c:
            with pytest.raises(ApprovalRequiredError):
                await c.post_review_comment(REPO, 42, other, approval=approval)

    async def test_write_tool_cannot_be_called_bare(self) -> None:
        """Even bypassing the public method, the transport refuses an unauthorised write."""
        async with client() as c:
            with pytest.raises(ApprovalRequiredError):
                await c._call("add_issue_comment", {"owner": "a", "repo": "b"})

    async def test_summary_comment_requires_an_approval(self) -> None:
        async with client() as c:
            with pytest.raises(ApprovalRequiredError):
                await c.post_summary_comment(REPO, 42, "summary", approvals=[])


class TestFailureHandling:
    async def test_upstream_error_becomes_a_domain_error(self) -> None:
        async with client(mode="erroring") as c:
            with pytest.raises(CodeHostError):
                await c.get_pull_request(REPO, 42)

    async def test_unconnected_client_refuses_calls(self) -> None:
        with pytest.raises(CodeHostError, match="not connected"):
            await client().get_pull_request(REPO, 42)

    async def test_unstartable_server_becomes_a_domain_error(self) -> None:
        bad = GitHubMcpClient(
            command=sys.executable,
            args=["-c", "raise SystemExit(1)"],
            token="ghp_x",
            logger=RecordingLogger(),
        )
        with pytest.raises(CodeHostError):
            async with bad:
                pass


class TestTokenHandling:
    async def test_token_is_not_passed_in_argv(self) -> None:
        """Guardrail G12: argv is world-readable via ``ps``."""
        secret = "ghp_thisexactstringmustnotappear00000"
        c = client(token=secret)

        assert not any(secret in arg for arg in c._args)
        assert secret not in c._command

    async def test_token_never_reaches_a_log_line(self) -> None:
        secret = "ghp_thisexactstringmustnotappear00000"
        logger = RecordingLogger()
        async with client(token=secret, logger=logger) as c:
            await c.get_pull_request(REPO, 42)

        for value in logger.all_field_values():
            assert secret not in str(value)


class TestRendering:
    def test_comment_body_carries_a_checkable_citation(self) -> None:
        finding = a_finding()
        body = render_finding(finding)

        assert "docs/security.md" in body
        assert "Sessions > Expiry" in body
        assert str(finding.citation.chunk_id) in body
