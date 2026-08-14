"""Allowlist properties. Guardrail G3, and OWASP LLM07 / Agentic A08.

These are cheap tests guarding an expensive mistake: the whole "what can this agent do to my
GitHub account?" answer lives in one frozenset, and the failure mode of getting it wrong is
silent until it isn't.
"""

from __future__ import annotations

import pytest

from app.infrastructure.mcp.allowlist import (
    ALLOWED_TOOLS,
    READ_TOOLS,
    WRITE_TOOLS,
    is_allowed,
    is_write,
    missing_tools,
    unexpected_tools,
)


def test_read_and_write_sets_are_disjoint() -> None:
    """A tool cannot be both. If one ever is, the write guard has a hole."""
    assert not (READ_TOOLS & WRITE_TOOLS)


def test_write_surface_is_exactly_three_tools() -> None:
    """The blast radius, stated as a number.

    Quorum can open+submit a pull request review comment (a three-call sequence on the real
    server: create, attach, submit) and post an issue comment. It cannot merge, close, push,
    or delete. If this test fails, the answer to "what can it do?" has changed and
    docs/Security.md needs revisiting.
    """
    assert {
        "pull_request_review_write",
        "add_comment_to_pending_review",
        "add_issue_comment",
    } == WRITE_TOOLS


@pytest.mark.parametrize(
    "tool",
    [
        "merge_pull_request",
        "delete_repository",
        "create_repository",
        "push_files",
        "update_pull_request_branch",
        "create_or_update_file",
        "fork_repository",
        "whoami",
        "",
        "get_pull_request; drop table",
    ],
)
def test_dangerous_and_unknown_tools_are_not_allowed(tool: str) -> None:
    assert not is_allowed(tool)


@pytest.mark.parametrize("tool", sorted(ALLOWED_TOOLS))
def test_allowlisted_tools_are_allowed(tool: str) -> None:
    assert is_allowed(tool)


def test_every_write_tool_is_classified_as_a_write() -> None:
    assert all(is_write(tool) for tool in WRITE_TOOLS)


def test_no_read_tool_is_classified_as_a_write() -> None:
    assert not any(is_write(tool) for tool in READ_TOOLS)


def test_unexpected_tools_reports_what_we_have_not_vetted() -> None:
    advertised = frozenset(ALLOWED_TOOLS | {"merge_pull_request", "delete_repository"})
    assert unexpected_tools(advertised) == {"merge_pull_request", "delete_repository"}


def test_unexpected_tools_is_empty_when_the_server_matches() -> None:
    assert unexpected_tools(frozenset(ALLOWED_TOOLS)) == frozenset()


def test_missing_tools_reports_read_tools_we_depend_on() -> None:
    advertised = frozenset(ALLOWED_TOOLS - {"pull_request_read"})
    assert missing_tools(advertised) == {"pull_request_read"}


def test_missing_tools_ignores_absent_write_tools() -> None:
    """A server without write tools is usable read-only; a server without the diff is not."""
    advertised = frozenset(READ_TOOLS)
    assert missing_tools(advertised) == frozenset()
