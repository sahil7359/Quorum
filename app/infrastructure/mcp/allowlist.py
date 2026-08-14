"""The MCP tool allowlist.

Guardrail G3. Quorum consumes a *third-party* MCP server that it does not control and that
can gain new tools in any release. Without an allowlist, "what can this agent do to my
GitHub account?" has the answer "whatever the server offers today", which is not an answer.

Two separate ideas live here and they must not be conflated:

* **Allowed** — Quorum is permitted to call this tool at all.
* **Write** — calling it changes something on GitHub, so it needs an approval.

A tool that is allowed is not thereby writable.
"""

from __future__ import annotations

from typing import Final

READ_TOOLS: Final[frozenset[str]] = frozenset(
    {
        # why: the real ghcr.io/github/github-mcp-server (v1.9.0, verified live against
        #      pallets/click) consolidated get_pull_request / get_pull_request_diff /
        #      get_pull_request_files into one method-dispatch tool. The granular names this
        #      set used to hold were never real -- they were taken from documentation and the
        #      live server refused to connect the first time this was checked, which is
        #      exactly the failure HANDOFF.md's risk #1 predicted.
        #      alt: keep the granular names and hope docs matched the server (they did not)
        "pull_request_read",
        "get_file_contents",
    }
)

WRITE_TOOLS: Final[frozenset[str]] = frozenset(
    {
        # why: same consolidation on the write side. Posting a review comment is now a
        #      three-call sequence -- pull_request_review_write(create), then
        #      add_comment_to_pending_review per finding, then
        #      pull_request_review_write(submit_pending) -- so all three tool names need to
        #      be allowlisted and writable, not just one.
        "pull_request_review_write",
        "add_comment_to_pending_review",
        "add_issue_comment",
    }
)

ALLOWED_TOOLS: Final[frozenset[str]] = READ_TOOLS | WRITE_TOOLS


def is_allowed(tool_name: str) -> bool:
    return tool_name in ALLOWED_TOOLS


def is_write(tool_name: str) -> bool:
    return tool_name in WRITE_TOOLS


def unexpected_tools(advertised: frozenset[str]) -> frozenset[str]:
    """Tools the server offers that we have not vetted.

    Not an error -- the GitHub MCP server legitimately exposes dozens of tools. It is logged
    at connect time so that a server growing a new capability is *visible* rather than
    discovered later. Guardrail A08.
    """
    return advertised - ALLOWED_TOOLS


def missing_tools(advertised: frozenset[str]) -> frozenset[str]:
    """Read tools we depend on that the server did not advertise.

    This *is* worth failing on: a review that silently skips the diff because the tool
    vanished would look like a clean review.
    """
    return READ_TOOLS - advertised
