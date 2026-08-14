"""Ports.

Every contract between a layer and the outside world, expressed as ``typing.Protocol`` —
structural, so an adapter satisfies a port without importing anything from ``domain`` to
inherit from. That is what keeps ``infrastructure`` free of a dependency on ``domain``'s
class hierarchy while still being checked against it by mypy.

Nothing here has an implementation. Nothing here imports a library.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from app.domain.entities import (
    Approval,
    AuditEvent,
    Chunk,
    Diff,
    Finding,
    PullRequest,
    RepoRef,
    Review,
    ScoredChunk,
)
from app.domain.values import BudgetState, ChunkId, FindingId, RunId, TokenUsage

# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One turn.

    ``role`` is a plain string rather than an enum because providers disagree about the set
    and the domain has no stake in the argument.
    """

    role: str
    content: str


@dataclass(frozen=True, slots=True)
class Completion:
    """A model response, paired with what it cost.

    Usage travels with the completion rather than being fetched separately so that a caller
    physically cannot record the output without recording the cost.
    """

    content: str
    usage: TokenUsage


@runtime_checkable
class ChatModelPort(Protocol):
    """A chat model. Implemented by Groq, Ollama and a deterministic fake."""

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        node: str,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        json_schema: Mapping[str, Any] | None = None,
    ) -> Completion: ...


# ---------------------------------------------------------------------------
# Code host
# ---------------------------------------------------------------------------


@runtime_checkable
class CodeHostPort(Protocol):
    """Read a pull request; write a comment back.

    The write methods take ``approval`` rather than being callable bare. Requiring the
    authorisation as an *argument* means an unauthorised write is not something you can
    forget to check — it is something you cannot express.
    """

    async def get_pull_request(self, repo: RepoRef, number: int) -> PullRequest: ...

    async def get_diff(self, repo: RepoRef, number: int, *, max_lines: int) -> Diff: ...

    async def get_file(self, repo: RepoRef, path: str, *, ref: str) -> str: ...

    async def post_review_comment(
        self,
        repo: RepoRef,
        number: int,
        finding: Finding,
        *,
        approval: Approval,
    ) -> str: ...

    async def post_summary_comment(
        self,
        repo: RepoRef,
        number: int,
        body: str,
        *,
        approvals: Sequence[Approval],
    ) -> str: ...


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


@runtime_checkable
class EmbedderPort(Protocol):
    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...

    @property
    def dimensions(self) -> int: ...


@runtime_checkable
class RerankerPort(Protocol):
    async def rerank(
        self, query: str, chunks: Sequence[ScoredChunk], *, top_k: int
    ) -> Sequence[ScoredChunk]: ...


@runtime_checkable
class RetrieverPort(Protocol):
    async def retrieve(
        self,
        query: str,
        *,
        repo: RepoRef,
        commit_sha: str,
        top_k: int,
    ) -> Sequence[ScoredChunk]: ...


@runtime_checkable
class ChunkStorePort(Protocol):
    async def upsert(
        self, chunks: Sequence[Chunk], embeddings: Sequence[Sequence[float]]
    ) -> int: ...

    async def get(self, chunk_id: ChunkId) -> Chunk | None: ...

    async def search_dense(
        self,
        embedding: Sequence[float],
        *,
        repo: RepoRef,
        commit_sha: str,
        limit: int,
    ) -> Sequence[ScoredChunk]: ...

    async def all_for_repo(self, repo: RepoRef, commit_sha: str) -> Sequence[Chunk]: ...


# ---------------------------------------------------------------------------
# Persistence: audit, cache, budget
# ---------------------------------------------------------------------------


@runtime_checkable
class AuditPort(Protocol):
    """Append-only. There is no update and no delete, by design and by database rule."""

    async def append(self, event: AuditEvent) -> None: ...

    async def history(self, run_id: RunId) -> Sequence[AuditEvent]: ...

    async def approval_for(self, finding_id: FindingId) -> Approval | None: ...


@runtime_checkable
class ReviewCachePort(Protocol):
    async def get(self, cache_key: str) -> Review | None: ...

    async def put(self, cache_key: str, review: Review) -> None: ...

    async def get_latest(self, repo: RepoRef, pr_number: int) -> Review | None:
        """The most recent cached review for this PR, regardless of which commit it was at.

        why: promoted from infrastructure in Phase 8. The budget-exhaustion fallback in
        docs/AppFlow.md §3 -- "serve the most recent cached review with an honest banner" --
        cannot be built on ``get`` alone, because ``get`` needs the *current* head_sha and the
        whole point of the fallback is that we may not want to spend the tokens to confirm it.
        alt: keep the cache exact-key-only and drop the honest-banner fallback (simpler port,
        breaks a documented, guardrail-adjacent behaviour)
        """
        ...


@runtime_checkable
class BudgetPort(Protocol):
    async def state(self) -> BudgetState: ...

    async def record(self, run_id: RunId, usage: TokenUsage) -> None: ...


@runtime_checkable
class RateLimiterPort(Protocol):
    """The live-review cap, separate from the token budget on purpose.

    why: docs/TechSpec.md lists ``QUORUM_LIVE_REVIEWS_PER_DAY`` as its own row, distinct from
    the daily token budget -- a cheap PR (few files, one specialist) and an expensive one both
    count as one live review against this cap, so it bounds *request volume* the way the token
    budget bounds *spend*. A single global counter, not per-IP: docs/Security.md T5 treats
    "someone hammers the live-review button" as a shared-resource problem, not a per-caller one.
    """

    async def try_acquire(self) -> bool:
        """Atomically check-and-increment. ``True`` if this call is under today's cap."""
        ...


# ---------------------------------------------------------------------------
# Observability — three mechanisms, deliberately three ports
# ---------------------------------------------------------------------------


@runtime_checkable
class LoggerPort(Protocol):
    """Structured logging.

    Every method takes an ``event`` name plus keyword fields, never a formatted string,
    because a formatted string cannot be queried. Implementations **must swallow their own
    exceptions**: telemetry never fails a request.
    """

    def debug(self, event: str, **fields: object) -> None: ...

    def info(self, event: str, **fields: object) -> None: ...

    def warn(self, event: str, **fields: object) -> None: ...

    def error(self, event: str, **fields: object) -> None: ...

    def bind(self, **fields: object) -> LoggerPort: ...


@runtime_checkable
class TracerPort(Protocol):
    """Latency and cost attribution across a run. Distinct from logging on purpose.

    A trace answers "where did the time and the tokens go?"; a log answers "what happened at
    14:03?". Conflating them means you cannot aggregate the first or grep the second.
    """

    def span(self, name: str, **attributes: object) -> AbstractContextManager[None]: ...


@runtime_checkable
class ClockPort(Protocol):
    """Injected so that tests are not a function of wall-clock time."""

    def now(self) -> datetime: ...
