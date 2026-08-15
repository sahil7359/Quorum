"""Fakes implementing domain ports.

Fakes, not mocks. A fake implements the Protocol, so mypy checks it against the real
contract; a ``Mock`` accepts any call you make and therefore proves nothing about whether
production code is using the port correctly.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.domain.entities import Diff, PullRequest, RepoRef, ScoredChunk
from app.domain.errors import CodeHostError
from app.domain.ports import ChatMessage, Completion, LoggerPort
from app.domain.values import TokenUsage


@dataclass
class LogLine:
    level: str
    event: str
    fields: dict[str, object]


@dataclass
class RecordingLogger:
    """Captures structured log events so tests can assert on decisions, not text."""

    lines: list[LogLine] = field(default_factory=list)
    bound: dict[str, object] = field(default_factory=dict)

    def _record(self, level: str, event: str, fields: dict[str, object]) -> None:
        self.lines.append(LogLine(level=level, event=event, fields={**self.bound, **fields}))

    def debug(self, event: str, **fields: object) -> None:
        self._record("DEBUG", event, fields)

    def info(self, event: str, **fields: object) -> None:
        self._record("INFO", event, fields)

    def warn(self, event: str, **fields: object) -> None:
        self._record("WARN", event, fields)

    def error(self, event: str, **fields: object) -> None:
        self._record("ERROR", event, fields)

    def bind(self, **fields: object) -> LoggerPort:
        return RecordingLogger(lines=self.lines, bound={**self.bound, **fields})

    # -- test helpers -------------------------------------------------------

    def events(self, level: str | None = None) -> list[str]:
        return [line.event for line in self.lines if level is None or line.level == level]

    def find(self, event: str) -> LogLine | None:
        return next((line for line in self.lines if line.event == event), None)

    def all_field_values(self) -> list[object]:
        """Every value logged anywhere -- used to assert secrets never reach a log line."""
        return [value for line in self.lines for value in line.fields.values()]


class NullTracer:
    @contextmanager
    def span(self, name: str, **attributes: object) -> Iterator[None]:
        yield


@dataclass
class FrozenClock:
    moment: datetime = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.moment


@dataclass
class FakeChatModel:
    """Deterministic ``ChatModelPort``.

    Responses are queued per node name, so a test can give the router one answer and each
    specialist another without patching anything. Records every call, because "what was the
    model actually asked?" is the question most specialist bugs come down to.
    """

    responses: dict[str, list[str]] = field(default_factory=dict)
    default: str = '{"findings": []}'
    calls: list[tuple[str, list[ChatMessage]]] = field(default_factory=list)
    fail_on: set[str] = field(default_factory=set)

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        node: str,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        json_schema: Mapping[str, object] | None = None,
    ) -> Completion:
        self.calls.append((node, list(messages)))
        if node in self.fail_on:
            raise RuntimeError(f"simulated provider failure for {node}")

        queued = self.responses.get(node)
        content = queued.pop(0) if queued else self.default
        return Completion(
            content=content,
            usage=TokenUsage(
                provider="fake",
                model=model or "fake-model",
                node=node,
                prompt_tokens=sum(len(m.content) // 4 for m in messages),
                output_tokens=len(content) // 4,
                latency_ms=1,
            ),
        )

    def prompt_for(self, node: str) -> str:
        """The full rendered prompt for a node, for asserting on fencing."""
        for called, messages in self.calls:
            if called == node:
                return "\n".join(m.content for m in messages)
        raise AssertionError(f"{node} was never called; called: {[c for c, _ in self.calls]}")


@dataclass
class FakeRetriever:
    """``RetrieverPort`` returning a fixed set of chunks, recording every query."""

    chunks: list[ScoredChunk] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    fail: bool = False
    per_specialist: dict[str, list[ScoredChunk]] = field(default_factory=dict)

    async def retrieve(
        self, query: str, *, repo: object, commit_sha: str, top_k: int
    ) -> Sequence[ScoredChunk]:
        self.queries.append(query)
        if self.fail:
            raise RuntimeError("simulated retrieval failure")
        for marker, chunks in self.per_specialist.items():
            if marker in query:
                return chunks[:top_k]
        return self.chunks[:top_k]


@dataclass
class FakeCodeHost:
    """``CodeHostPort`` serving fixed PR data. Also satisfies ``DocIngestionPort`` (adds
    ``list_markdown_files`` to the same ``get_file`` it already has) -- one fake for both,
    since the real adapter (``GitHubMcpClient``) is the same object satisfying both too."""

    pull_request: PullRequest | None = None
    diff: Diff | None = None
    files: dict[str, str] = field(default_factory=dict)
    file_errors: set[str] = field(default_factory=set)
    markdown_files: list[str] = field(default_factory=list)
    posted: list[tuple[str, object]] = field(default_factory=list)

    async def get_pull_request(self, repo: RepoRef, number: int) -> PullRequest:
        assert self.pull_request is not None
        return self.pull_request

    async def get_diff(self, repo: RepoRef, number: int, *, max_lines: int) -> Diff:
        assert self.diff is not None
        return self.diff

    async def list_markdown_files(self, repo: RepoRef, *, limit: int = 60) -> Sequence[str]:
        return tuple(self.markdown_files[:limit])

    async def get_file(self, repo: RepoRef, path: str, *, ref: str) -> str:
        if path in self.file_errors:
            raise CodeHostError(f"cannot fetch {path}")
        return self.files.get(path, "")

    async def post_review_comment(
        self, repo: RepoRef, number: int, finding: object, *, approval: object
    ) -> str:
        """Records the post. A test asserting "nothing was posted" needs somewhere to look."""
        self.posted.append((str(getattr(finding, "finding_id", "?")), approval))
        return f"comment-{len(self.posted)}"

    async def post_summary_comment(
        self, repo: RepoRef, number: int, body: str, *, approvals: Sequence[object]
    ) -> str:
        self.posted.append(("summary", body))
        return f"issue-comment-{len(self.posted)}"
