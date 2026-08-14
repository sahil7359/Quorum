"""Entities.

The load-bearing design decision in this module is the split between
:class:`CandidateFinding` and :class:`Finding`.

A specialist produces *candidates* -- what the model said, citation optional, because a
model can and will return a finding with no citation or an invented one. A :class:`Finding`
**cannot be constructed without a resolvable** :class:`Citation`. So "cite-or-drop" is not a
rule enforced by remembering to check: it is a type transition that can fail, and the only
way from candidate to finding is through a function that drops what it cannot ground.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.domain.values import (
    ApprovalAction,
    AuditAction,
    ChunkId,
    ChunkLocator,
    FindingId,
    RunId,
    RunStatus,
    Severity,
    SpecialistKind,
)

# ---------------------------------------------------------------------------
# The pull request under review
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RepoRef:
    """``owner/name``. Validated on construction because it reaches a URL eventually."""

    owner: str
    name: str

    def __post_init__(self) -> None:
        for part, label in ((self.owner, "owner"), (self.name, "name")):
            if not part or "/" in part or ".." in part:
                raise ValueError(f"invalid repo {label}: {part!r}")

    @classmethod
    def parse(cls, value: str) -> RepoRef:
        owner, sep, name = value.partition("/")
        if not sep:
            raise ValueError(f"expected 'owner/name', got {value!r}")
        return cls(owner=owner, name=name)

    def __str__(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True, slots=True)
class DiffHunk:
    """One contiguous changed region of one file."""

    file_path: str
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    content: str

    @property
    def added_line_count(self) -> int:
        return sum(
            1 for line in self.content.splitlines() if line.startswith("+") and line != "+++"
        )


@dataclass(frozen=True, slots=True)
class ChangedFile:
    file_path: str
    status: str
    additions: int
    deletions: int
    hunks: tuple[DiffHunk, ...] = ()

    @property
    def is_documentation(self) -> bool:
        """Prose, not code.

        why: without this, the "source changed but no tests" heuristic fires on a
        README-only pull request and summons the test-coverage reviewer to comment on
        prose. Found by a test that expected an empty floor and got one.
        alt: treat every non-test file as code (simpler, wrong on documentation PRs)
        """
        lowered = self.file_path.lower().replace("\\", "/")
        return lowered.endswith((".md", ".rst", ".txt", ".adoc")) or lowered.startswith("docs/")

    @property
    def is_code_file(self) -> bool:
        return not self.is_test_file and not self.is_documentation

    @property
    def is_test_file(self) -> bool:
        """Heuristic, and deliberately a broad one.

        Used by the routing floor: source changed with no test file changed is a signal for
        the test-coverage specialist. False positives here cost one extra specialist call;
        false negatives cost a missed review. Biased towards over-matching on purpose.
        """
        parts = self.file_path.lower().replace("\\", "/").split("/")
        directories, stem = parts[:-1], parts[-1]
        return (
            bool({"test", "tests", "spec", "specs", "__tests__"} & set(directories))
            or stem.startswith("test_")
            # why: matches Button.test.tsx, api.spec.js and every other extension in that
            #      family without enumerating them. Enumerating is how .test.tsx got missed
            #      the first time -- the list said .test.ts and the file ended .test.tsx.
            #      alt: an extension tuple (explicit, and I already got it wrong once)
            or ".test." in stem
            or ".spec." in stem
            or stem.endswith(("_test.py", "_spec.py", "_test.go"))
        )


@dataclass(frozen=True, slots=True)
class Diff:
    """The changed content of a pull request.

    ``truncated`` is carried on the entity rather than handled at the edges because a
    silently truncated review is a lie about coverage -- whoever renders this must be able
    to say so.
    """

    files: tuple[ChangedFile, ...]
    raw: str
    truncated: bool = False
    truncated_at_line: int | None = None

    @property
    def total_lines(self) -> int:
        return self.raw.count("\n") + 1 if self.raw else 0

    @property
    def added_lines(self) -> int:
        return sum(f.additions for f in self.files)

    @property
    def touched_paths(self) -> tuple[str, ...]:
        return tuple(f.file_path for f in self.files)

    @property
    def has_test_changes(self) -> bool:
        return any(f.is_test_file for f in self.files)

    @property
    def has_source_changes(self) -> bool:
        return any(not f.is_test_file for f in self.files)

    @property
    def has_code_changes(self) -> bool:
        """Changes to files that could plausibly need a test. Excludes documentation."""
        return any(f.is_code_file for f in self.files)


@dataclass(frozen=True, slots=True)
class PullRequest:
    repo: RepoRef
    number: int
    title: str
    body: str
    author: str
    base_sha: str
    head_sha: str
    url: str = ""

    def __post_init__(self) -> None:
        if self.number <= 0:
            raise ValueError(f"pull request number must be positive, got {self.number}")


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Chunk:
    """A retrievable unit of the target repository's own documentation."""

    chunk_id: ChunkId
    locator: ChunkLocator
    content: str
    start_line: int
    end_line: int
    ordinal: int
    token_count: int
    heading_level: int = 0

    def __post_init__(self) -> None:
        # why: an id that does not derive from its own locator means the chunker and the
        #      id scheme have drifted apart, and every citation downstream is unverifiable.
        #      Cheap to check here, impossible to detect later.
        #      alt: trust the constructor caller (faster, silently corruptible)
        if not self.chunk_id.matches(self.locator):
            raise ValueError(
                f"chunk id {self.chunk_id} was not derived from its locator "
                f"{self.locator.canonical()!r}"
            )

    @classmethod
    def create(
        cls,
        locator: ChunkLocator,
        content: str,
        *,
        start_line: int,
        end_line: int,
        ordinal: int,
        token_count: int,
        heading_level: int = 0,
    ) -> Chunk:
        return cls(
            chunk_id=ChunkId.derive(locator),
            locator=locator,
            content=content,
            start_line=start_line,
            end_line=end_line,
            ordinal=ordinal,
            token_count=token_count,
            heading_level=heading_level,
        )


@dataclass(frozen=True, slots=True)
class ScoredChunk:
    chunk: Chunk
    score: float
    dense_rank: int | None = None
    sparse_rank: int | None = None
    rerank_score: float | None = None

    @property
    def chunk_id(self) -> ChunkId:
        return self.chunk.chunk_id


# ---------------------------------------------------------------------------
# Findings — the candidate/confirmed split
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Citation:
    """Proof that a finding is grounded in a specific piece of the repository's own docs.

    Note what this does and does not assert. It asserts the chunk exists, that the id
    derives from the locator, and that the chunk was actually returned to the specialist
    that cited it. It does **not** assert the chunk *supports* the finding. Grounding is
    checkable; aptness is not. Stated here because it is the honest limit of the invariant.
    """

    chunk_id: ChunkId
    locator: ChunkLocator
    quote: str = ""

    def __post_init__(self) -> None:
        if not self.chunk_id.matches(self.locator):
            raise ValueError("citation chunk id does not derive from its locator")

    @property
    def display(self) -> str:
        return f"{self.locator.file_path} — {self.locator.section_path}"


@dataclass(frozen=True, slots=True)
class CandidateFinding:
    """What a specialist model actually returned, before grounding is verified.

    ``chunk_id`` is a bare ``str | None`` on purpose: it is untrusted model output and may
    be absent, malformed, or invented. It is not a :class:`ChunkId` because constructing one
    would already be an assertion about validity that has not been made yet.
    """

    specialist: SpecialistKind
    severity: Severity
    confidence: float
    title: str
    body: str
    chunk_id: str | None = None
    file_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence}")


@dataclass(frozen=True, slots=True)
class Finding:
    """A finding that survived grounding. **Cannot exist without a citation.**

    There is no ``citation: Citation | None`` here and there never will be. Making the field
    optional would turn cite-or-drop from something the type system guarantees into
    something a reviewer has to notice.
    """

    finding_id: FindingId
    specialist: SpecialistKind
    severity: Severity
    confidence: float
    title: str
    body: str
    citation: Citation
    file_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence}")

    @property
    def payload_hash(self) -> str:
        """Binds an approval to exact text.

        Edit a finding after it was approved and this changes, so the publish guard refuses
        it and the human is asked again. Guardrail G5.
        """
        material = "\x1f".join(
            [
                self.title,
                self.body,
                self.severity.value,
                self.specialist.value,
                str(self.citation.chunk_id),
                self.file_path or "",
                str(self.line_start or ""),
                str(self.line_end or ""),
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @property
    def rank_key(self) -> tuple[int, float]:
        """Severity first, then confidence. Never the string ordering of severity."""
        return (self.severity.rank, self.confidence)


# ---------------------------------------------------------------------------
# The review
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """Which specialists ran, and **why**.

    ``reason`` is persisted, not merely logged, because specialist routing accuracy is a
    published metric and logs are sampled and rotated. ``heuristic_floor`` is recorded
    separately from ``llm_added`` so that a bad routing call can be attributed to the
    heuristics or to the model, rather than to "the router".
    """

    specialists: tuple[SpecialistKind, ...]
    reason: str
    heuristic_floor: tuple[SpecialistKind, ...] = ()
    llm_added: tuple[SpecialistKind, ...] = ()
    llm_removal_ignored: tuple[SpecialistKind, ...] = ()

    def __post_init__(self) -> None:
        if SpecialistKind.CORRECTNESS not in self.specialists:
            raise ValueError(
                "correctness is unconditional: a diff that warrants no correctness review "
                "is a diff Quorum should not have been asked about"
            )
        if not self.reason.strip():
            raise ValueError("a routing decision without a reason is not debuggable")


@dataclass(frozen=True, slots=True)
class Review:
    run_id: RunId
    repo: RepoRef
    pr_number: int
    head_sha: str
    status: RunStatus
    routing: RoutingDecision
    findings: tuple[Finding, ...] = ()
    diff_truncated: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None

    @property
    def ranked_findings(self) -> tuple[Finding, ...]:
        return tuple(sorted(self.findings, key=lambda f: f.rank_key, reverse=True))

    @property
    def citation_rate(self) -> float:
        """1.0 by construction -- a ``Finding`` cannot exist without a ``Citation``.

        Computed rather than hardcoded so that if the type ever loosens, the number moves
        instead of the claim quietly becoming false.
        """
        if not self.findings:
            return 1.0
        cited = sum(1 for f in self.findings if f.citation is not None)
        return cited / len(self.findings)


# ---------------------------------------------------------------------------
# Approval and audit
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Approval:
    run_id: RunId
    finding_id: FindingId
    action: ApprovalAction
    actor: str
    payload_hash: str
    note: str = ""
    created_at: datetime | None = None

    def authorises(self, finding: Finding) -> bool:
        """The publish guard's question, asked of the approval rather than of the graph.

        Both the identity *and* the exact text must match. An approval for a finding that
        was subsequently edited does not authorise the edited text.
        """
        return (
            self.action is ApprovalAction.APPROVED
            and self.finding_id == finding.finding_id
            and self.payload_hash == finding.payload_hash
        )


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One immutable row in the append-only audit table.

    Audit is a table, not a log stream, because logs rotate and the answer to "why did it
    post that?" must outlive log retention.
    """

    run_id: RunId
    action: AuditAction
    actor: str
    finding_id: FindingId | None = None
    payload_hash: str | None = None
    detail: dict[str, str] = field(default_factory=dict)
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CacheKey:
    """``sha256(repo, pr, head_sha, config_hash)``.

    ``config_hash`` is supplied by the caller (it lives in settings, which the domain must
    not read). Including it means a prompt change invalidates the cache rather than serving
    a review the current code would not produce.
    """

    repo: RepoRef
    pr_number: int
    head_sha: str
    config_hash: str

    def value(self) -> str:
        material = f"{self.repo}|{self.pr_number}|{self.head_sha}|{self.config_hash}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


def rank_findings(findings: Sequence[Finding]) -> tuple[Finding, ...]:
    return tuple(sorted(findings, key=lambda f: f.rank_key, reverse=True))
