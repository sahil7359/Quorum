"""A fake GitHub MCP server -- built with the real MCP SDK, spoken over real stdio.

This is the substitute for a `GITHUB_TOKEN` I do not have, and it is a better test than a
mocked client would be. The transport is genuine: a subprocess, a JSON-RPC handshake, tool
discovery, and structured results. Everything except GitHub itself is real, so the parts I
could actually get wrong -- protocol handling, result unwrapping, error propagation -- are
exercised rather than stubbed.

Behaviour is switched by environment variable so one script covers several scenarios:

* ``FAKE_MCP_MODE=normal``    -- the full vetted tool set (default)
* ``FAKE_MCP_MODE=missing``   -- omits ``get_pull_request_diff``; the client must refuse to run
* ``FAKE_MCP_MODE=extra``     -- advertises unvetted tools including a destructive one
* ``FAKE_MCP_MODE=erroring``  -- every tool returns an error result

Run directly: ``python -m tests.support.fake_github_mcp_server``
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.mcpserver import MCPServer

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


def _fail_if_erroring() -> None:
    if MODE == "erroring":
        raise RuntimeError("simulated upstream failure")


@server.tool()
def get_pull_request(owner: str, repo: str, pullNumber: int) -> dict[str, Any]:  # noqa: N803 - GitHub's parameter name
    _fail_if_erroring()
    return {
        "title": "Add token issuance",
        "body": "Issues a token on login.",
        "user": {"login": "octocat"},
        "base": {"sha": "base1234"},
        "head": {"sha": "head5678"},
        "html_url": f"https://github.com/{owner}/{repo}/pull/{pullNumber}",
    }


@server.tool()
def get_pull_request_files(owner: str, repo: str, pullNumber: int) -> list[dict[str, Any]]:  # noqa: N803
    _fail_if_erroring()
    return [
        {"filename": "app/auth/login.py", "status": "modified", "additions": 3, "deletions": 1},
        {"filename": "README.md", "status": "modified", "additions": 1, "deletions": 0},
    ]


@server.tool()
def get_file_contents(owner: str, repo: str, path: str, ref: str) -> str:
    _fail_if_erroring()
    return f"# contents of {path} at {ref}\n"


@server.tool()
def add_pull_request_review_comment(
    owner: str,
    repo: str,
    pullNumber: int,  # noqa: N803
    body: str,
    path: str,
    line: int,
) -> str:
    _fail_if_erroring()
    posted_comments.append({"path": path, "line": line, "body": body})
    return f"comment-{len(posted_comments)}"


@server.tool()
def add_issue_comment(owner: str, repo: str, issueNumber: int, body: str) -> str:  # noqa: N803
    _fail_if_erroring()
    posted_comments.append({"issue": issueNumber, "body": body})
    return f"issue-comment-{len(posted_comments)}"


@server.tool()
def whoami() -> str:
    """Not on Quorum's allowlist. Exists so a test can prove the client refuses it."""
    return "octocat"


if MODE != "missing":

    @server.tool()
    def get_pull_request_diff(owner: str, repo: str, pullNumber: int) -> str:  # noqa: N803
        _fail_if_erroring()
        return SAMPLE_DIFF


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
