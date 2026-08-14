"""Value-object invariants, including the four chunk-identity rules from docs/Schema.md §2.1.

Chunk identity is the one scheme in this project that cannot be corrected later: every
retrieval number published depends on chunks being addressable at chunk level rather than
file level. These tests are the guard.
"""

from __future__ import annotations

import pytest

from app.domain.values import (
    CHUNK_ID_LENGTH,
    BudgetState,
    ChunkId,
    ChunkLocator,
    RunId,
    Severity,
    SpecialistKind,
    TokenUsage,
)


def locator(**overrides: object) -> ChunkLocator:
    base: dict[str, object] = {
        "repo": "psf/requests",
        "commit_sha": "a1b2c3d4",
        "file_path": "docs/architecture.md",
        "section_path": "Design > Retrieval",
        "start_offset": 100,
        "end_offset": 400,
    }
    base.update(overrides)
    return ChunkLocator(**base)  # type: ignore[arg-type]  # keyword dict is homogeneous by construction


class TestChunkIdentity:
    def test_offsets_disambiguate_chunk_ids(self) -> None:
        """Same repo, same commit, same file, same section — different offsets.

        This is *the* file-level-id trap. If ids were derived from the file alone, these two
        chunks would collide and every citation would point at a whole document.
        """
        first = ChunkId.derive(locator(start_offset=0, end_offset=300))
        second = ChunkId.derive(locator(start_offset=300, end_offset=600))

        assert first != second

    def test_chunk_ids_are_stable_across_reingest(self) -> None:
        """Re-ingesting the same commit must produce identical ids, or the cache is useless."""
        assert ChunkId.derive(locator()) == ChunkId.derive(locator())

    def test_commit_sha_participates_in_chunk_id(self) -> None:
        """Chunks are per-commit. The same text at a different SHA is a different chunk."""
        assert ChunkId.derive(locator(commit_sha="aaaa")) != ChunkId.derive(
            locator(commit_sha="bbbb")
        )

    def test_chunk_id_verifies_against_its_locator(self) -> None:
        """A chunk id is a hash, not a reversible encoding.

        You cannot recover the locator from the id. Resolution goes the other way: the
        locator is stored in columns beside the id, and ``matches`` proves the pair is
        consistent. That is what makes a citation checkable by a human.
        """
        loc = locator()
        chunk_id = ChunkId.derive(loc)

        assert chunk_id.matches(loc)
        assert not chunk_id.matches(locator(start_offset=1))

    def test_section_path_participates(self) -> None:
        assert ChunkId.derive(locator(section_path="A > B")) != ChunkId.derive(
            locator(section_path="A > C")
        )

    def test_file_path_participates(self) -> None:
        assert ChunkId.derive(locator(file_path="a.md")) != ChunkId.derive(
            locator(file_path="b.md")
        )

    def test_id_is_short_lowercase_hex(self) -> None:
        value = ChunkId.derive(locator()).value
        assert len(value) == CHUNK_ID_LENGTH
        assert value == value.lower()
        assert all(c in "0123456789abcdef" for c in value)

    @pytest.mark.parametrize("bad", ["", "tooshort", "z" * CHUNK_ID_LENGTH, "A" * CHUNK_ID_LENGTH])
    def test_malformed_ids_are_rejected(self, bad: str) -> None:
        """Model output reaches this constructor. It must not accept garbage."""
        with pytest.raises(ValueError):
            ChunkId(bad)

    def test_canonical_form_is_the_documented_one(self) -> None:
        """The hashed string is frozen. Changing it invalidates the whole corpus."""
        assert locator().canonical() == (
            "psf/requests@a1b2c3d4:docs/architecture.md#Design > Retrieval@100-400"
        )


class TestLocatorValidation:
    def test_reversed_offsets_rejected(self) -> None:
        with pytest.raises(ValueError, match="precedes"):
            locator(start_offset=500, end_offset=100)

    def test_negative_offset_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be >= 0"):
            locator(start_offset=-1)

    @pytest.mark.parametrize("field", ["repo", "commit_sha", "file_path"])
    def test_required_fields(self, field: str) -> None:
        with pytest.raises(ValueError, match="required"):
            locator(**{field: ""})


class TestSeverity:
    def test_rank_order(self) -> None:
        assert Severity.INFO.rank < Severity.LOW.rank < Severity.MEDIUM.rank < Severity.HIGH.rank

    def test_alphabetical_order_is_not_severity_order(self) -> None:
        """The reason ``rank`` exists at all.

        Sorted as strings: high < info < low < medium. Exactly backwards. This test is here
        so that anyone who later reaches for ``sorted(findings, key=...severity)`` finds out
        immediately rather than shipping a review that buries its worst finding.
        """
        alphabetical = sorted(Severity, key=lambda s: s.value)
        by_rank = sorted(Severity, key=lambda s: s.rank)

        assert alphabetical != by_rank
        assert alphabetical[0] is Severity.HIGH
        assert by_rank[-1] is Severity.HIGH

    def test_serialises_as_its_string(self) -> None:
        assert f"{Severity.HIGH}" == "high"


class TestSpecialistKind:
    def test_exactly_three_specialists(self) -> None:
        """v1 is locked at three. A fourth requires eval evidence that it earns its cost."""
        assert set(SpecialistKind) == {
            SpecialistKind.CORRECTNESS,
            SpecialistKind.SECURITY,
            SpecialistKind.TEST_COVERAGE,
        }

    def test_unknown_specialist_cannot_be_constructed(self) -> None:
        """Guardrail A07: the supervisor selects from a closed set; it cannot invent one."""
        with pytest.raises(ValueError):
            SpecialistKind("documentation")


class TestUsageAndBudget:
    def test_total_tokens(self) -> None:
        usage = TokenUsage(
            provider="ollama",
            model="llama3.1:8b",
            node="route",
            prompt_tokens=120,
            output_tokens=30,
            latency_ms=900,
        )
        assert usage.total_tokens == 150

    def test_negative_tokens_rejected(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            TokenUsage(
                provider="p", model="m", node="n", prompt_tokens=-1, output_tokens=0, latency_ms=0
            )

    def test_budget_exhaustion(self) -> None:
        assert BudgetState(consumed=100_000, limit=100_000).exhausted
        assert not BudgetState(consumed=99_999, limit=100_000).exhausted
        assert BudgetState(consumed=120_000, limit=100_000).remaining == 0


def test_run_ids_are_unique() -> None:
    assert RunId.new() != RunId.new()
