"""Value objects.

Frozen, hashable, and — where it matters — *not* bare strings. A ``ChunkId`` that is a
``str`` is a bug waiting to be passed where a ``FindingId`` was expected, and the type
checker will not notice.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Self

# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, order=True)
class RunId:
    """Correlation id for one review, threaded through every log line, span and audit row."""

    value: str

    @classmethod
    def new(cls) -> Self:
        return cls(str(uuid.uuid4()))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class FindingId:
    value: str

    @classmethod
    def new(cls) -> Self:
        return cls(str(uuid.uuid4()))

    def __str__(self) -> str:
        return self.value


CHUNK_ID_LENGTH: Final[int] = 16
"""16 hex characters = 64 bits.

At the scale of six repositories (order 1e4 chunks) collision probability is ~1e-11, and the
UNIQUE constraint on the locator tuple in Postgres would catch one anyway. Short enough to
read in a log line, which matters because these ids appear in every citation.
"""


@dataclass(frozen=True, slots=True, order=True)
class ChunkLocator:
    """Where a chunk came from, precisely enough to render a link a human can follow.

    This is the tuple that makes citations meaningful. ``section_path`` is the full heading
    breadcrumb -- "Design > Retrieval > Hybrid search" means something to a reader;
    "chunk 47" does not.
    """

    repo: str
    commit_sha: str
    file_path: str
    section_path: str
    start_offset: int
    end_offset: int

    def __post_init__(self) -> None:
        if self.start_offset < 0:
            raise ValueError(f"start_offset must be >= 0, got {self.start_offset}")
        if self.end_offset < self.start_offset:
            raise ValueError(
                f"end_offset {self.end_offset} precedes start_offset {self.start_offset}"
            )
        if not self.repo or not self.commit_sha or not self.file_path:
            raise ValueError("repo, commit_sha and file_path are all required")

    def canonical(self) -> str:
        """The exact string hashed to produce a ``ChunkId``.

        Format is frozen. Changing it changes every chunk id in the corpus, which
        invalidates every stored embedding and every published retrieval number -- so a
        change here is a re-ingest, and ``Settings.chunker_version`` must move with it.
        """
        return (
            f"{self.repo}@{self.commit_sha}:{self.file_path}"
            f"#{self.section_path}@{self.start_offset}-{self.end_offset}"
        )


@dataclass(frozen=True, slots=True, order=True)
class ChunkId:
    """Identity of one chunk.

    **Chunk-level, never file-level.** Derived from the full locator including byte offsets,
    so two chunks from the same file and the same section are still distinguishable. A
    file-level id would silently invalidate every retrieval number this project publishes.

    The id is a *hash*, not a reversible encoding: you cannot recover the locator from the
    id alone. Resolution goes the other way -- the locator is stored in columns alongside
    the id, and ``matches()`` verifies that the pair is consistent.
    """

    value: str

    def __post_init__(self) -> None:
        if len(self.value) != CHUNK_ID_LENGTH:
            raise ValueError(f"chunk id must be {CHUNK_ID_LENGTH} chars, got {self.value!r}")
        if not all(c in "0123456789abcdef" for c in self.value):
            raise ValueError(f"chunk id must be lowercase hex, got {self.value!r}")

    @classmethod
    def derive(cls, locator: ChunkLocator) -> Self:
        digest = hashlib.sha256(locator.canonical().encode("utf-8")).hexdigest()
        return cls(digest[:CHUNK_ID_LENGTH])

    def matches(self, locator: ChunkLocator) -> bool:
        """True if this id was derived from that locator. The verification direction."""
        return self == ChunkId.derive(locator)

    def __str__(self) -> str:
        return self.value


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class Severity(StrEnum):
    """How much a finding matters.

    ``StrEnum`` so it serialises to the database as-is, but **never sort on the string**:
    alphabetically "high" < "info" < "low" < "medium", which is precisely backwards. Sort on
    :attr:`rank`. ``test_alphabetical_order_is_not_severity_order`` exists to stop anyone
    (including me, later) reaching for the default comparison.
    """

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK: Final[dict[Severity, int]] = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
}


class SpecialistKind(StrEnum):
    """The reviewer roles. A closed set, deliberately.

    Guardrail A07 (agent orchestration exploitation): the supervisor selects *from* this
    enum and cannot invent a specialist, because there is nothing to invent. A fourth
    specialist requires eval evidence that it earns its cost -- see docs/PRD.md section 6.
    """

    CORRECTNESS = "correctness"
    SECURITY = "security"
    TEST_COVERAGE = "test_coverage"


class ApprovalAction(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    EDITED = "edited"


class AuditAction(StrEnum):
    """Every state a finding can pass through, including the ones we refuse."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EDITED = "edited"
    POSTED = "posted"
    REFUSED = "refused"


class RunStatus(StrEnum):
    RUNNING = "running"
    PROPOSED = "proposed"
    PUBLISHED = "published"
    REJECTED = "rejected"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Usage accounting
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """One LLM call's cost, recorded as fact rather than estimated.

    The daily budget is derived by summing these, not by decrementing a counter. A counter
    drifts from reality the first time a call fails after the decrement.
    """

    provider: str
    model: str
    node: str
    prompt_tokens: int
    output_tokens: int
    latency_ms: int
    finish_reason: str | None = None

    def __post_init__(self) -> None:
        if self.prompt_tokens < 0 or self.output_tokens < 0:
            raise ValueError("token counts cannot be negative")

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class BudgetState:
    """Where we stand against the daily cap."""

    consumed: int
    limit: int

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.consumed)

    @property
    def exhausted(self) -> bool:
        return self.consumed >= self.limit
