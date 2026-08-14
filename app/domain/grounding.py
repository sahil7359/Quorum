"""Cite-or-drop: turning untrusted model output into grounded findings.

This is the most important function in the system and it is deliberately pure — no I/O, no
model, no framework — so that the invariant everything else rests on can be tested
exhaustively in microseconds.

Three ways a candidate fails to become a finding:

1. **No citation at all.** The model returned a finding with no ``chunk_id``.
2. **Unresolvable citation.** The ``chunk_id`` is malformed, or names a chunk that does not
   exist in the corpus.
3. **Hallucinated-but-real citation.** The ``chunk_id`` is a genuine chunk, but it was not
   among those returned to *that specialist* for *that query*. This is the subtle one: a
   model that has seen chunk ids in an earlier turn can cite a real chunk it was never
   shown, which would pass a naive "does this id exist?" check.

Case 3 is why the function takes ``visible`` per specialist rather than a global corpus.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from app.domain.entities import CandidateFinding, Chunk, Citation, Finding
from app.domain.values import ChunkId, FindingId, SpecialistKind


class DropReason:
    """Why a candidate was dropped. Strings because they are logged and counted, not matched."""

    NO_CITATION = "no_citation"
    MALFORMED_CHUNK_ID = "malformed_chunk_id"
    UNKNOWN_CHUNK_ID = "unknown_chunk_id"
    CHUNK_NOT_VISIBLE_TO_SPECIALIST = "chunk_not_visible_to_specialist"


@dataclass(frozen=True, slots=True)
class DroppedCandidate:
    candidate: CandidateFinding
    reason: str


@dataclass(frozen=True, slots=True)
class GroundingResult:
    """Findings that survived, and an accounting of everything that did not.

    The dropped list is not debris — it is the numerator of "how often does the model try to
    invent a citation?", which is worth knowing and is otherwise invisible.
    """

    findings: tuple[Finding, ...]
    dropped: tuple[DroppedCandidate, ...]

    @property
    def drop_rate(self) -> float:
        total = len(self.findings) + len(self.dropped)
        return len(self.dropped) / total if total else 0.0

    def dropped_for(self, reason: str) -> tuple[DroppedCandidate, ...]:
        return tuple(d for d in self.dropped if d.reason == reason)


def ground_candidates(
    candidates: Iterable[CandidateFinding],
    *,
    corpus: Mapping[ChunkId, Chunk],
    visible: Mapping[SpecialistKind, Sequence[ChunkId]],
) -> GroundingResult:
    """Drop every candidate that cannot be grounded, and build ``Finding`` for the rest.

    Args:
        candidates: raw specialist output, untrusted.
        corpus: every chunk that exists, by id.
        visible: for each specialist, the chunk ids actually returned to it by retrieval.

    Returns:
        Survivors and an itemised account of the drops.
    """
    findings: list[Finding] = []
    dropped: list[DroppedCandidate] = []

    for candidate in candidates:
        if not candidate.chunk_id:
            dropped.append(DroppedCandidate(candidate, DropReason.NO_CITATION))
            continue

        try:
            chunk_id = ChunkId(candidate.chunk_id.strip().lower())
        except ValueError:
            dropped.append(DroppedCandidate(candidate, DropReason.MALFORMED_CHUNK_ID))
            continue

        chunk = corpus.get(chunk_id)
        if chunk is None:
            dropped.append(DroppedCandidate(candidate, DropReason.UNKNOWN_CHUNK_ID))
            continue

        # why: checked per specialist, not against the whole corpus. A model can cite a real
        #      chunk it was never shown; a global existence check would let that through.
        #      alt: global membership test (simpler, misses the interesting failure)
        if chunk_id not in visible.get(candidate.specialist, ()):
            dropped.append(DroppedCandidate(candidate, DropReason.CHUNK_NOT_VISIBLE_TO_SPECIALIST))
            continue

        findings.append(
            Finding(
                finding_id=FindingId.new(),
                specialist=candidate.specialist,
                severity=candidate.severity,
                confidence=candidate.confidence,
                title=candidate.title,
                body=candidate.body,
                citation=Citation(chunk_id=chunk.chunk_id, locator=chunk.locator),
                file_path=candidate.file_path,
                line_start=candidate.line_start,
                line_end=candidate.line_end,
            )
        )

    return GroundingResult(findings=tuple(findings), dropped=tuple(dropped))


def deduplicate(findings: Sequence[Finding]) -> tuple[Finding, ...]:
    """Collapse findings that several specialists noticed independently.

    Two findings are the same if they cite the same chunk and point at the same location in
    the diff. The survivor is the one with the highest severity, then confidence — so
    agreement between specialists raises prominence rather than producing three comments.
    """
    best: dict[tuple[str, str | None, int | None], Finding] = {}
    order: list[tuple[str, str | None, int | None]] = []

    for finding in findings:
        key = (str(finding.citation.chunk_id), finding.file_path, finding.line_start)
        existing = best.get(key)
        if existing is None:
            best[key] = finding
            order.append(key)
        elif finding.rank_key > existing.rank_key:
            best[key] = finding

    return tuple(best[key] for key in order)
