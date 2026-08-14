"""Cite-or-drop.

The invariant everything else in Quorum rests on: **a finding that reaches a human carries a
citation that resolves to a chunk the specialist was actually shown.**

Four ways a candidate fails, each tested separately. The interesting one is the fourth — a
model citing a *real* chunk it was never given, which a naive "does this id exist?" check
would wave through.
"""

from __future__ import annotations

import pytest

from app.domain.entities import CandidateFinding, Chunk, Finding
from app.domain.grounding import DropReason, deduplicate, ground_candidates
from app.domain.values import ChunkId, ChunkLocator, FindingId, Severity, SpecialistKind


def make_chunk(offset: int, *, file_path: str = "docs/style.md") -> Chunk:
    return Chunk.create(
        locator=ChunkLocator(
            repo="acme/widget",
            commit_sha="deadbeef",
            file_path=file_path,
            section_path="Conventions",
            start_offset=offset,
            end_offset=offset + 200,
        ),
        content=f"content at {offset}",
        start_line=offset // 10,
        end_line=offset // 10 + 5,
        ordinal=offset // 200,
        token_count=40,
    )


def candidate(
    *,
    chunk_id: str | None,
    specialist: SpecialistKind = SpecialistKind.CORRECTNESS,
    severity: Severity = Severity.MEDIUM,
    title: str = "Something is wrong",
) -> CandidateFinding:
    return CandidateFinding(
        specialist=specialist,
        severity=severity,
        confidence=0.8,
        title=title,
        body="body",
        chunk_id=chunk_id,
    )


@pytest.fixture
def corpus() -> dict[ChunkId, Chunk]:
    chunks = [make_chunk(0), make_chunk(200), make_chunk(400)]
    return {c.chunk_id: c for c in chunks}


class TestCiteOrDrop:
    def test_grounded_candidate_becomes_a_finding(self, corpus: dict[ChunkId, Chunk]) -> None:
        shown = next(iter(corpus))
        result = ground_candidates(
            [candidate(chunk_id=str(shown))],
            corpus=corpus,
            visible={SpecialistKind.CORRECTNESS: [shown]},
        )

        assert len(result.findings) == 1
        assert result.findings[0].citation.chunk_id == shown
        assert not result.dropped

    def test_uncited_finding_is_dropped(self, corpus: dict[ChunkId, Chunk]) -> None:
        result = ground_candidates(
            [candidate(chunk_id=None)],
            corpus=corpus,
            visible={SpecialistKind.CORRECTNESS: list(corpus)},
        )

        assert not result.findings
        assert result.dropped_for(DropReason.NO_CITATION)

    @pytest.mark.parametrize("bad_id", ["", "not-hex", "abc", "z" * 16, "  "])
    def test_malformed_chunk_id_is_dropped(self, corpus: dict[ChunkId, Chunk], bad_id: str) -> None:
        result = ground_candidates(
            [candidate(chunk_id=bad_id)],
            corpus=corpus,
            visible={SpecialistKind.CORRECTNESS: list(corpus)},
        )

        assert not result.findings

    def test_unknown_chunk_id_is_dropped(self, corpus: dict[ChunkId, Chunk]) -> None:
        """Well-formed, plausible, and not in the corpus. The classic hallucination."""
        result = ground_candidates(
            [candidate(chunk_id="0123456789abcdef")],
            corpus=corpus,
            visible={SpecialistKind.CORRECTNESS: list(corpus)},
        )

        assert not result.findings
        assert result.dropped_for(DropReason.UNKNOWN_CHUNK_ID)

    def test_real_chunk_not_shown_to_this_specialist_is_dropped(
        self, corpus: dict[ChunkId, Chunk]
    ) -> None:
        """The subtle case, and the reason ``visible`` is per specialist.

        The cited chunk genuinely exists. It was retrieved — for a *different* specialist. A
        global "does this id exist?" check passes this through; per-specialist visibility
        does not.
        """
        ids = list(corpus)
        result = ground_candidates(
            [candidate(chunk_id=str(ids[2]), specialist=SpecialistKind.SECURITY)],
            corpus=corpus,
            visible={
                SpecialistKind.SECURITY: [ids[0]],
                SpecialistKind.CORRECTNESS: [ids[2]],
            },
        )

        assert not result.findings
        assert result.dropped_for(DropReason.CHUNK_NOT_VISIBLE_TO_SPECIALIST)

    def test_specialist_with_no_retrieval_grounds_nothing(
        self, corpus: dict[ChunkId, Chunk]
    ) -> None:
        """Retrieval returning nothing means the specialist stays silent.

        Silence is the correct failure mode for a grounded reviewer. It cannot invent a
        finding because there is nothing for it to cite.
        """
        result = ground_candidates(
            [candidate(chunk_id=str(next(iter(corpus))))],
            corpus=corpus,
            visible={},
        )

        assert not result.findings

    def test_case_and_whitespace_in_model_output_are_tolerated(
        self, corpus: dict[ChunkId, Chunk]
    ) -> None:
        """Normalising sloppy formatting is fine. Inventing an id is not."""
        shown = next(iter(corpus))
        result = ground_candidates(
            [candidate(chunk_id=f"  {str(shown).upper()}  ")],
            corpus=corpus,
            visible={SpecialistKind.CORRECTNESS: [shown]},
        )

        assert len(result.findings) == 1

    def test_drop_rate_is_reported(self, corpus: dict[ChunkId, Chunk]) -> None:
        """How often the model tries to invent a citation is worth knowing, not just fixing."""
        shown = next(iter(corpus))
        result = ground_candidates(
            [
                candidate(chunk_id=str(shown)),
                candidate(chunk_id=None),
                candidate(chunk_id="0123456789abcdef"),
                candidate(chunk_id=str(shown)),
            ],
            corpus=corpus,
            visible={SpecialistKind.CORRECTNESS: [shown]},
        )

        assert len(result.findings) == 2
        assert result.drop_rate == 0.5


class TestFindingCannotExistWithoutCitation:
    def test_the_type_forbids_it(self) -> None:
        """Cite-or-drop is a type transition, not a rule someone must remember to apply.

        ``Finding`` has no ``citation: Citation | None``. Constructing one without a
        citation is a TypeError, which means the invariant holds even if every caller is
        wrong.
        """
        with pytest.raises(TypeError):
            Finding(  # type: ignore[call-arg]  # deliberately omitting the citation
                finding_id=FindingId.new(),
                specialist=SpecialistKind.CORRECTNESS,
                severity=Severity.HIGH,
                confidence=0.9,
                title="t",
                body="b",
            )


class TestDeduplicate:
    def test_same_chunk_and_location_collapses_to_highest_severity(
        self, corpus: dict[ChunkId, Chunk]
    ) -> None:
        shown = next(iter(corpus))
        result = ground_candidates(
            [
                candidate(chunk_id=str(shown), severity=Severity.LOW, title="low"),
                candidate(
                    chunk_id=str(shown),
                    specialist=SpecialistKind.SECURITY,
                    severity=Severity.HIGH,
                    title="high",
                ),
            ],
            corpus=corpus,
            visible={
                SpecialistKind.CORRECTNESS: [shown],
                SpecialistKind.SECURITY: [shown],
            },
        )

        deduped = deduplicate(result.findings)

        assert len(deduped) == 1
        assert deduped[0].severity is Severity.HIGH

    def test_different_chunks_are_kept(self, corpus: dict[ChunkId, Chunk]) -> None:
        ids = list(corpus)
        result = ground_candidates(
            [candidate(chunk_id=str(ids[0])), candidate(chunk_id=str(ids[1]))],
            corpus=corpus,
            visible={SpecialistKind.CORRECTNESS: ids},
        )

        assert len(deduplicate(result.findings)) == 2

    def test_order_is_stable(self, corpus: dict[ChunkId, Chunk]) -> None:
        ids = list(corpus)
        result = ground_candidates(
            [candidate(chunk_id=str(cid), title=f"t{i}") for i, cid in enumerate(ids)],
            corpus=corpus,
            visible={SpecialistKind.CORRECTNESS: ids},
        )

        assert [f.title for f in deduplicate(result.findings)] == ["t0", "t1", "t2"]
