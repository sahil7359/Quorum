"""A fake GitHub MCP server -- built with the real MCP SDK, spoken over real stdio.

This is the substitute for a `GITHUB_TOKEN` I do not have, and it is a better test than a
mocked client would be. The transport is genuine: a subprocess, a JSON-RPC handshake, tool
discovery, and structured results. Everything except GitHub itself is real, so the parts I
could actually get wrong -- protocol handling, result unwrapping, error propagation -- are
exercised rather than stubbed.

Tool names and the ``pull_request_read``/``pull_request_review_write`` method-dispatch shape
mirror the real ``ghcr.io/github/github-mcp-server`` v1.9.0, confirmed by a live run against
``pallets/click#3728`` (``eval/smoke/live_github.py``) -- not the granular per-action tool
names an earlier version of this fake used, which turned out not to exist on the real server.

Behaviour is switched by environment variable so one script covers several scenarios:

* ``FAKE_MCP_MODE=normal``    -- the full vetted tool set (default)
* ``FAKE_MCP_MODE=missing``   -- omits ``get_file_contents``; the client must refuse to run
* ``FAKE_MCP_MODE=extra``     -- advertises unvetted tools including a destructive one
* ``FAKE_MCP_MODE=erroring``  -- every tool returns an error result

Run directly: ``python -m tests.support.fake_github_mcp_server``
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import EmbeddedResource, TextContent, TextResourceContents

MODE = os.environ.get("FAKE_MCP_MODE", "normal")
TOKEN_SEEN = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "")

SAMPLE_DIFF = """diff --git a/app/auth/login.py b/app/auth/login.py
index 1111111..2222222 100644
--- a/app/auth/login.py
+++ b/app/auth/login.py
@@ -10,6 +10,9 @@ def authenticate(user, password):
     if not user:
         return None
-    return check(password)
+    token = issue_token(user)
+    token.expires_at = None
+    return token
diff --git a/README.md b/README.md
index 3333333..4444444 100644
--- a/README.md
+++ b/README.md
@@ -1,2 +1,3 @@
 # Widget
+A line about widgets.
"""

server = MCPServer(name="fake-github-mcp", version="0.0.1")

posted_comments: list[dict[str, Any]] = []
pending_review_open = False
"""Mirrors the real server's one-pending-review-per-user constraint closely enough to catch
a client that calls add_comment_to_pending_review or submit_pending without first opening one."""


def _fail_if_erroring() -> None:
    if MODE == "erroring":
        raise RuntimeError("simulated upstream failure")


@server.tool()
def pull_request_read(
    owner: str,
    repo: str,
    pullNumber: int,  # noqa: N803 - GitHub's parameter name
    method: str,
    after: str | None = None,
) -> Any:
    """Method-dispatch tool, mirroring the real server's consolidated PR-read surface."""
    _fail_if_erroring()
    if method == "get":
        return {
            "title": "Add token issuance",
            "body": "Issues a token on login.",
            "user": {"login": "octocat"},
            "base": {"sha": "base1234"},
            "head": {"sha": "head5678"},
            "html_url": f"https://github.com/{owner}/{repo}/pull/{pullNumber}",
        }
    if method == "get_diff":
        return SAMPLE_DIFF
    if method == "get_files":
        return [
            {"filename": "app/auth/login.py", "status": "modified", "additions": 3, "deletions": 1},
            {"filename": "README.md", "status": "modified", "additions": 1, "deletions": 0},
        ]
    raise ValueError(f"fake server does not implement pull_request_read method {method!r}")


if MODE != "missing":

    @server.tool()
    def get_file_contents(owner: str, repo: str, path: str, ref: str) -> list[Any]:
        # why: mirrors the real server's two-block reply -- a TextContent confirmation plus
        #      an EmbeddedResource carrying the actual bytes -- discovered live against
        #      pallets/click. An earlier version of this fake returned a plain string, which
        #      let get_file's original bug (reading only the confirmation block) pass every
        #      test while being wrong against the real server.
        _fail_if_erroring()
        content = f"# contents of {path} at {ref}\n"
        return [
            TextContent(
                type="text", text=f"successfully downloaded text file (SHA: fake{len(content)})"
            ),
            EmbeddedResource(
                type="resource",
                resource=TextResourceContents(
                    uri=f"repo://{owner}/{repo}/sha/{ref}/contents/{path}",
                    mime_type="text/plain; charset=utf-8",
                    text=content,
                ),
            ),
        ]


@server.tool()
def search_code(
    query: str,
    fields: list[str] | None = None,
    perPage: int | None = None,  # noqa: N803 - GitHub's parameter name
) -> dict[str, Any]:
    """Mirrors the real server's response shape for ``extension:md repo:owner/name`` --
    confirmed live against ``psf/black`` before this fake was written to match it."""
    _fail_if_erroring()
    paths = ["README.md", "docs/index.md", "docs/faq.md"]
    return {
        "incomplete_results": False,
        "items": [{"path": p} for p in paths],
        "total_count": len(paths),
    }


@server.tool()
def pull_request_review_write(
    owner: str,
    repo: str,
    pullNumber: int,  # noqa: N803 - GitHub's parameter name
    method: str,
    body: str | None = None,
    event: str | None = None,
) -> dict[str, Any]:
    """Method-dispatch tool for opening and submitting a review, mirroring the real server."""
    global pending_review_open
    _fail_if_erroring()
    if method == "create":
        pending_review_open = True
        return {"id": "review-1", "state": "PENDING"}
    if method == "submit_pending":
        if not pending_review_open:
            raise ValueError("no pending review to submit")
        pending_review_open = False
        return {"id": "review-1", "state": event or "COMMENTED"}
    raise ValueError(f"fake server does not implement pull_request_review_write method {method!r}")


@server.tool()
def add_comment_to_pending_review(
    owner: str,
    repo: str,
    pullNumber: int,  # noqa: N803
    body: str,
    path: str,
    line: int,
    side: str | None = None,
) -> dict[str, Any]:
    _fail_if_erroring()
    if not pending_review_open:
        raise ValueError("no pending review to attach a comment to")
    posted_comments.append({"path": path, "line": line, "body": body})
    return {"id": f"comment-{len(posted_comments)}"}


@server.tool()
def add_issue_comment(owner: str, repo: str, issueNumber: int, body: str) -> str:  # noqa: N803
    _fail_if_erroring()
    posted_comments.append({"issue": issueNumber, "body": body})
    return f"issue-comment-{len(posted_comments)}"


@server.tool()
def whoami() -> str:
    """Not on Quorum's allowlist. Exists so a test can prove the client refuses it."""
    return "octocat"


if MODE == "extra":

    @server.tool()
    def delete_repository(owner: str, repo: str) -> str:
        """Deliberately alarming. Never reachable: it is not on the allowlist.

        Present so that ``test_destructive_tool_is_refused_even_when_advertised`` is testing
        a real advertised tool rather than a hypothetical one.
        """
        return "deleted"

    @server.tool()
    def merge_pull_request(owner: str, repo: str, pullNumber: int) -> str:  # noqa: N803
        return "merged"


if __name__ == "__main__":
    server.run(transport="stdio")
