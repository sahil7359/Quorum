"""GitHub, consumed through the official GitHub MCP server.

Transport is stdio: the server runs as a subprocess (Docker, in production) and the token
reaches it through the **environment, never argv**, because argv is world-readable via
``ps``. Guardrail G12.

Three guards live in this module, and they are deliberately in the *client* rather than in
the graph that calls it:

1. **Allowlist** (G3) -- a tool outside the vetted set is refused before it is sent.
2. **Write authorisation** (G4) -- a write tool cannot be reached without an ``Approval``
   that authorises the exact finding, verified here and not merely upstream.
3. **Advertised-tool inspection** (A08) -- at connect time we compare what the server offers
   against what we vetted, fail on missing read tools and log unexpected new ones.

Putting these in the graph would make them a property of one call path. Putting them here
makes them a property of the client, which is what "the agent cannot do X" has to mean.
"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from types import TracebackType
from typing import Any, Self

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.domain import log_events
from app.domain.entities import Approval, ChangedFile, Diff, Finding, PullRequest, RepoRef
from app.domain.errors import ApprovalRequiredError, CodeHostError, ToolNotAllowedError
from app.domain.ports import LoggerPort
from app.infrastructure.mcp.allowlist import (
    is_allowed,
    is_write,
    missing_tools,
    unexpected_tools,
)
from app.infrastructure.mcp.diff_parser import parse_unified_diff


class GitHubMcpClient:
    """Adapter satisfying ``CodeHostPort``.

    Structural typing means this class never imports the port, but mypy still checks it
    against the Protocol wherever one is expected.
    """

    def __init__(
        self,
        *,
        command: str,
        args: list[str],
        token: str,
        logger: LoggerPort,
        env: dict[str, str] | None = None,
    ) -> None:
        self._command = command
        self._args = args
        self._token = token
        self._logger = logger
        self._extra_env = env or {}
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    # -- connection ---------------------------------------------------------

    async def __aenter__(self) -> Self:
        # why: the token is placed in the child's environment, never in self._args. argv is
        #      visible to any process on the host via `ps`, and a leaked PAT in a process
        #      listing is not something a log redactor can undo.
        #      alt: pass --token on the command line (simpler wiring, leaks the credential)
        env = {"GITHUB_PERSONAL_ACCESS_TOKEN": self._token, **self._extra_env}
        params = StdioServerParameters(command=self._command, args=self._args, env=env)

        self._stack = AsyncExitStack()
        try:
            read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            init = await session.initialize()
            self._session = session
        except Exception as exc:
            await self._stack.aclose()
            self._stack = None
            raise CodeHostError(f"could not start MCP server {self._command!r}: {exc}") from exc

        await self._inspect_advertised_tools(session, server_name=init.server_info.name)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._session = None

    async def _inspect_advertised_tools(self, session: ClientSession, *, server_name: str) -> None:
        listing = await session.list_tools()
        advertised = frozenset(tool.name for tool in listing.tools)

        missing = missing_tools(advertised)
        if missing:
            raise CodeHostError(
                f"MCP server {server_name!r} does not advertise required tools: "
                f"{sorted(missing)}. Refusing to run -- a review that silently skips the "
                "diff would look like a clean review."
            )

        unexpected = unexpected_tools(advertised)
        if unexpected:
            # Not an error: the GitHub MCP server legitimately exposes dozens of tools we do
            # not use. Logged so that a server gaining capability is visible, not surprising.
            self._logger.info(
                log_events.MCP_TOOLS_UNVETTED,
                server=server_name,
                count=len(unexpected),
                sample=sorted(unexpected)[:10],
            )

        self._logger.info(log_events.MCP_CONNECTED, server=server_name, advertised=len(advertised))

    # -- tool invocation ----------------------------------------------------

    async def _call(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        write_authorised: bool = False,
    ) -> Any:
        if not is_allowed(tool):
            self._logger.error(log_events.MCP_TOOL_REFUSED, tool=tool, reason="not_allowlisted")
            raise ToolNotAllowedError(tool)

        if is_write(tool) and not write_authorised:
            self._logger.error(log_events.MCP_TOOL_REFUSED, tool=tool, reason="no_approval")
            raise ApprovalRequiredError(f"write tool {tool!r} called without an approval")

        if self._session is None:
            raise CodeHostError("client is not connected; use `async with GitHubMcpClient(...)`")

        self._logger.debug(log_events.MCP_TOOL_CALL, tool=tool, arguments=sorted(arguments))
        result = await self._session.call_tool(tool, arguments)

        if result.is_error:
            detail = _first_text(result.content) or "unknown error"
            raise CodeHostError(f"{tool} failed: {detail}")

        if result.structured_content is not None:
            payload = result.structured_content
            # The SDK wraps scalar returns as {"result": ...}; unwrap so callers see the value.
            return payload.get("result", payload) if isinstance(payload, dict) else payload
        return _first_text(result.content)

    # -- reads --------------------------------------------------------------

    async def get_pull_request(self, repo: RepoRef, number: int) -> PullRequest:
        raw = await self._call(
            "get_pull_request", {"owner": repo.owner, "repo": repo.name, "pullNumber": number}
        )
        data = _as_mapping(raw, "get_pull_request")
        try:
            return PullRequest(
                repo=repo,
                number=number,
                title=str(data.get("title", "")),
                body=str(data.get("body") or ""),
                author=str(data.get("user", {}).get("login", "") if data.get("user") else ""),
                base_sha=str(data["base"]["sha"]),
                head_sha=str(data["head"]["sha"]),
                url=str(data.get("html_url", "")),
            )
        except (KeyError, TypeError) as exc:
            raise CodeHostError(f"malformed pull request payload: {exc}") from exc

    async def get_diff(self, repo: RepoRef, number: int, *, max_lines: int) -> Diff:
        raw = await self._call(
            "get_pull_request_diff",
            {"owner": repo.owner, "repo": repo.name, "pullNumber": number},
        )
        if not isinstance(raw, str):
            raise CodeHostError("get_pull_request_diff did not return a unified diff")

        diff = parse_unified_diff(raw, max_lines=max_lines)
        if diff.truncated:
            # WARN, not INFO: degraded but handled. The truncation also rides on the entity
            # so that whoever renders the review can say what it did not see.
            self._logger.warn(
                log_events.DIFF_TRUNCATED,
                repo=str(repo),
                pr=number,
                limit=max_lines,
                files=len(diff.files),
            )
        return diff

    async def get_file(self, repo: RepoRef, path: str, *, ref: str) -> str:
        raw = await self._call(
            "get_file_contents",
            {"owner": repo.owner, "repo": repo.name, "path": path, "ref": ref},
        )
        if isinstance(raw, str):
            return raw
        data = _as_mapping(raw, "get_file_contents")
        return str(data.get("content", ""))

    async def list_changed_files(self, repo: RepoRef, number: int) -> tuple[ChangedFile, ...]:
        raw = await self._call(
            "get_pull_request_files",
            {"owner": repo.owner, "repo": repo.name, "pullNumber": number},
        )
        items = raw if isinstance(raw, list) else []
        return tuple(
            ChangedFile(
                file_path=str(item.get("filename", "")),
                status=str(item.get("status", "modified")),
                additions=int(item.get("additions", 0)),
                deletions=int(item.get("deletions", 0)),
            )
            for item in items
        )

    # -- writes -------------------------------------------------------------

    async def post_review_comment(
        self,
        repo: RepoRef,
        number: int,
        finding: Finding,
        *,
        approval: Approval,
    ) -> str:
        # why: the guard asks the approval whether it authorises THIS finding, rather than
        #      trusting that the graph only routes here after approval. Graph topology is a
        #      convention; a guard is not. Checks identity AND payload_hash, so text edited
        #      after approval loses its authorisation.
        #      alt: rely on the publish node being the only caller (one bug from a leak)
        if not approval.authorises(finding):
            self._logger.error(
                log_events.PUBLISH_REFUSED,
                finding_id=str(finding.finding_id),
                reason="approval_does_not_authorise",
            )
            raise ApprovalRequiredError(f"approval does not authorise finding {finding.finding_id}")

        comment_id = await self._call(
            "add_pull_request_review_comment",
            {
                "owner": repo.owner,
                "repo": repo.name,
                "pullNumber": number,
                "body": render_finding(finding),
                "path": finding.file_path or "",
                "line": finding.line_start or 1,
            },
            write_authorised=True,
        )
        self._logger.info(
            log_events.PUBLISH_POSTED,
            finding_id=str(finding.finding_id),
            chunk_id=str(finding.citation.chunk_id),
        )
        return str(comment_id)

    async def post_summary_comment(
        self,
        repo: RepoRef,
        number: int,
        body: str,
        *,
        approvals: list[Approval],
    ) -> str:
        if not approvals:
            raise ApprovalRequiredError("summary comment requires at least one approval")

        comment_id = await self._call(
            "add_issue_comment",
            {"owner": repo.owner, "repo": repo.name, "issueNumber": number, "body": body},
            write_authorised=True,
        )
        return str(comment_id)


def render_finding(finding: Finding) -> str:
    """The comment body, with the citation rendered so a human can check the grounding.

    The citation line is not decoration. It is the difference between "a bot says this" and
    "a bot says this, and here is the rule in your own repository it is appealing to".
    """
    citation = finding.citation
    return (
        f"**{finding.severity.value.upper()} · {finding.specialist.value}** — {finding.title}\n\n"
        f"{finding.body}\n\n"
        f"> Grounded in `{citation.locator.file_path}` — {citation.locator.section_path}\n"
        f"> (chunk `{citation.chunk_id}` @ bytes "
        f"{citation.locator.start_offset}-{citation.locator.end_offset})\n"
    )


def _first_text(content: list[Any]) -> str | None:
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            return text
    return None


def _as_mapping(raw: Any, tool: str) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CodeHostError(f"{tool} returned non-JSON text") from exc
        if isinstance(parsed, dict):
            return parsed
    raise CodeHostError(f"{tool} returned an unexpected payload type: {type(raw).__name__}")
