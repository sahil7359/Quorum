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

### What is and is not verified against the real server

The four **read** paths (``get_pull_request``, ``get_diff``, ``get_file``,
``list_changed_files``) were verified live against ``ghcr.io/github/github-mcp-server`` v1.9.0
on a real merged PR (``pallets/click#3728``) -- see ``eval/smoke/live_github.py``. That first
run failed: the tool names this module originally assumed
(``get_pull_request``/``get_pull_request_diff``/``get_pull_request_files``) do not exist on
the real server, which consolidated them into one method-dispatch tool, ``pull_request_read``.
This module and the allowlist were corrected to match; the fake server and its tests were
updated in tandem.

``list_markdown_files`` (``search_code``) was verified live too, against ``psf/black`` --
returned the same 39 markdown files a direct, unauthenticated GitHub REST call found
independently, confirming the query syntax and response shape before building the ingestion
pipeline on top of it.

``post_review_comment``'s three-call write sequence (``pull_request_review_write`` create ->
``add_comment_to_pending_review`` -> ``pull_request_review_write`` submit_pending) is written
against the real server's *documented* input schema, discovered from the same live
``tools/list`` response, but **has not itself been called against the real server** -- doing
so would post a real, visible comment on someone else's repository, which is not something to
do without deliberately choosing a throwaway PR to do it against. Treat the exact shape of
``add_comment_to_pending_review``'s return value as unverified until it is.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
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
            await self._inspect_advertised_tools(session, server_name=init.server_info.name)
        except Exception as exc:
            # why: __aenter__ raising means __aexit__ is never called (the `async with`
            #      protocol only invokes it on a successful enter), so cleanup has to happen
            #      here or the child process and its stdio pipes leak. A leaked subprocess
            #      isn't just a resource left behind: the next event-loop shutdown that tries
            #      to cancel its still-running read task blocks forever waiting for a process
            #      that nothing ever asked to exit.
            await self._stack.aclose()
            self._stack = None
            self._session = None
            if isinstance(exc, CodeHostError):
                raise
            raise CodeHostError(f"could not start MCP server {self._command!r}: {exc}") from exc

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

    async def _call_result(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        write_authorised: bool = False,
    ) -> Any:
        """Guarded ``call_tool``, returning the raw ``CallToolResult``.

        Split from :meth:`_call` so :meth:`get_file` can inspect every content block itself
        -- the real server puts a file's actual bytes in an ``EmbeddedResource`` block, not
        the ``TextContent`` block every other tool uses, and unwrapping to a single value too
        early would throw that block away before ``get_file`` ever saw it.
        """
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
        return result

    async def _call(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        write_authorised: bool = False,
    ) -> Any:
        result = await self._call_result(tool, arguments, write_authorised=write_authorised)
        if result.structured_content is not None:
            payload = result.structured_content
            # The SDK wraps scalar returns as {"result": ...}; unwrap so callers see the value.
            return payload.get("result", payload) if isinstance(payload, dict) else payload
        return _unwrap_text_content(result.content)

    # -- reads --------------------------------------------------------------

    async def _pull_request_read(
        self, method: str, repo: RepoRef, number: int, **extra: Any
    ) -> Any:
        # why: pull_request_read is a single method-dispatch tool covering what used to be
        #      three separate tools (get_pull_request, get_pull_request_diff,
        #      get_pull_request_files) on the real server. One private helper keeps the
        #      dispatch string in one place rather than repeated at each call site.
        return await self._call(
            "pull_request_read",
            {
                "owner": repo.owner,
                "repo": repo.name,
                "pullNumber": number,
                "method": method,
                **extra,
            },
        )

    async def get_pull_request(self, repo: RepoRef, number: int) -> PullRequest:
        raw = await self._pull_request_read("get", repo, number)
        data = _as_mapping(raw, "pull_request_read(get)")
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
        raw = await self._pull_request_read("get_diff", repo, number)
        if not isinstance(raw, str):
            raise CodeHostError("pull_request_read(get_diff) did not return a unified diff")

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
        result = await self._call_result(
            "get_file_contents",
            {"owner": repo.owner, "repo": repo.name, "path": path, "ref": ref},
        )

        embedded = _extract_embedded_file_text(result.content)
        if embedded is not None:
            return embedded

        # Fallback for a server (real or fake) that answers with a plain string or a JSON
        # mapping instead of an EmbeddedResource -- both shapes were true at different times
        # of this client's own history, see the module docstring.
        if result.structured_content is not None:
            payload = result.structured_content
            payload = payload.get("result", payload) if isinstance(payload, dict) else payload
        else:
            payload = _unwrap_text_content(result.content)
        if isinstance(payload, str):
            return payload
        data = _as_mapping(payload, "get_file_contents")
        return str(data.get("content", ""))

    async def list_markdown_files(self, repo: RepoRef, *, limit: int = 60) -> tuple[str, ...]:
        """Adapter satisfying ``DocIngestionPort``.

        why ``search_code`` rather than walking ``get_file_contents`` directory by directory:
        pointed at a directory, ``get_file_contents`` returns that directory's immediate
        entries (confirmed live, against ``psf/black``'s ``docs/`` -- a JSON array of
        ``{type, name, path, ...}``), which means discovering every markdown file in a repo
        would cost one call per directory level, unbounded by repo shape. GitHub's code search
        (``extension:md repo:owner/name``) finds every match in one call. The tradeoff: code
        search only indexes a repo's default branch, not an arbitrary commit -- the *paths* it
        returns are current-branch, not necessarily what exists at the exact ``head_sha`` a
        review is running against. Content is always fetched fresh via ``get_file`` at the real
        commit afterward, so a stale path list can only ever miss a file, never serve stale
        *content*.
        alt: walk the tree via repeated get_file_contents (correct at any single ref, one round
        trip per directory -- too slow for a synchronous first-request ingestion path)

        ``limit`` caps at one page (GitHub's ``perPage`` max is 100) rather than paginating --
        a repo with more than a few dozen real markdown docs is already well past what a
        single review's retrieved-context budget can use, so a second API round trip to see
        the rest would cost latency for docs nothing downstream would ever surface.
        """
        raw = await self._call(
            "search_code",
            {
                "query": f"extension:md repo:{repo}",
                "perPage": min(limit, 100),
                "fields": ["path"],
            },
        )
        data = _as_mapping(raw, "search_code")
        items = data.get("items", [])
        paths = [str(item["path"]) for item in items if isinstance(item, dict) and "path" in item]
        return tuple(paths[:limit])

    async def list_changed_files(self, repo: RepoRef, number: int) -> tuple[ChangedFile, ...]:
        raw = await self._pull_request_read("get_files", repo, number)
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

        # why: the real server has no single "post a review comment" tool any more. Posting
        #      one is a three-call sequence: open a pending review, attach the comment to it,
        #      then submit. event="COMMENT" on submit -- never APPROVE or REQUEST_CHANGES --
        #      because Quorum comments on findings, it does not gate merges by voting on the
        #      PR itself; that is a decision this client must not make on a human's behalf.
        #      alt: submit with no event (leaves the review pending, comment stays invisible)
        args = {"owner": repo.owner, "repo": repo.name, "pullNumber": number}
        await self._call(
            "pull_request_review_write",
            {**args, "method": "create"},
            write_authorised=True,
        )
        comment_id = await self._call(
            "add_comment_to_pending_review",
            {
                **args,
                "body": render_finding(finding),
                "path": finding.file_path or "",
                "line": finding.line_start or 1,
                "side": "RIGHT",
            },
            write_authorised=True,
        )
        await self._call(
            "pull_request_review_write",
            {**args, "method": "submit_pending", "event": "COMMENT"},
            write_authorised=True,
        )
        self._logger.info(
            log_events.PUBLISH_POSTED,
            finding_id=str(finding.finding_id),
            chunk_id=str(finding.citation.chunk_id),
        )
        # The comment's id, if the payload is a mapping shaped like GitHub's REST comment
        # object; falls back to the raw stringified result otherwise. Unverified against the
        # real server -- see the module docstring's "what is NOT verified" note.
        if isinstance(comment_id, dict) and "id" in comment_id:
            return str(comment_id["id"])
        return str(comment_id)

    async def post_summary_comment(
        self,
        repo: RepoRef,
        number: int,
        body: str,
        *,
        approvals: Sequence[Approval],
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


def _maybe_json(text: str) -> Any:
    """Parse ``text`` as JSON when possible; otherwise return it unchanged.

    A unified diff is deliberately not JSON, and must come back as the raw string. A GitHub
    payload almost always is JSON. Trying and falling back, rather than asking the caller to
    know which, is what lets one unwrap path serve both.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _extract_embedded_file_text(content: list[Any]) -> str | None:
    """A file's real content, when the server embeds it as a resource rather than plain text.

    why: found live, against the real GitHub MCP server -- ``get_file_contents`` replies with
    **two** content blocks: a ``TextContent`` confirmation ("successfully downloaded text
    file...") and an ``EmbeddedResource`` whose ``resource.text`` (or, for binary files,
    ``resource.blob``, base64) holds the actual bytes. Taking the first text block, as the
    original unwrap did, silently returned the confirmation message instead of the file --
    caught because ``eval/smoke/live_github.py`` prints the actual bytes fetched, not just
    that the call succeeded. ``None`` here means "no embedded resource"; the caller falls
    back to treating the plain unwrap as the content, which is what a fake server without
    this resource-block behaviour still returns.
    alt: assume every server does this (the fake one built for tests, pre-fix, did not)
    """
    for block in content:
        resource = getattr(block, "resource", None)
        if resource is None:
            continue
        text = getattr(resource, "text", None)
        if isinstance(text, str):
            return text
        blob = getattr(resource, "blob", None)
        if isinstance(blob, str):
            return base64.b64decode(blob).decode("utf-8", errors="replace")
    return None


def _unwrap_text_content(content: list[Any]) -> Any:
    """Unwrap a tool result with no ``structured_content``.

    why: found live, against the fake GitHub MCP server -- when a tool's return type is not
    concretely typed enough for the SDK to build a structured-output schema (``pull_request_read``
    dispatches to several different shapes, so its return type is ``Any``), a *list* return
    value is serialised as one ``TextContent`` block per item, not one block holding a JSON
    array. The original unwrap took only the first block via ``_first_text``, which silently
    dropped every item after the first -- ``list_changed_files`` on a two-file diff returned
    one file. Whether the real Go-based server does the same is unverified, but dropping
    content blocks is wrong regardless of which server sent them, so this handles both a
    single block (parsed as JSON if it is JSON, else returned as the raw string -- the diff
    case) and multiple blocks (each parsed individually and returned as a list).
    alt: only fix the fake server's typing (masks the same bug if the real server ever
    chunks a response the same way)
    """
    texts = [block.text for block in content if isinstance(getattr(block, "text", None), str)]
    if not texts:
        return None
    if len(texts) == 1:
        return _maybe_json(texts[0])
    return [_maybe_json(text) for text in texts]


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
