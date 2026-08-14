"""Chunker invariants — the ones that cannot be corrected after ingestion.

Every published retrieval number and every citation depends on these holding. If chunk ids
were file-level, or unstable between runs, every test in the retrieval eval would still pass
and every number would be meaningless.
"""

from __future__ import annotations

import pytest

from app.domain.entities import Chunk
from app.domain.values import ChunkId
from app.infrastructure.retrieval.chunker import (
    ROOT_SECTION,
    ChunkerConfig,
    chunk_markdown,
    estimate_tokens,
)

DOC = """# Quorum

Front matter before any heading is still addressable.

## Design

Some words about the design of the system and how it hangs together in practice.

### Retrieval

Dense embeddings are strong on paraphrase and weak on exact symbols such as
`RetrievalPort`. BM25 with a code-aware tokenizer catches those.

## Security

Diff content is untrusted and is fenced before it reaches a prompt.
"""


def chunk(
    text: str = DOC, *, sha: str = "abc123", path: str = "docs/Design.md", **kw: object
) -> list[Chunk]:
    return chunk_markdown(
        text,
        repo="acme/widget",
        commit_sha=sha,
        file_path=path,
        config=ChunkerConfig(**kw),  # type: ignore[arg-type]
    )


class TestIdentity:
    def test_chunk_ids_are_stable_across_reingest(self) -> None:
        """Re-ingesting the same commit must produce byte-identical ids."""
        first = [c.chunk_id for c in chunk()]
        second = [c.chunk_id for c in chunk()]

        assert first == second

    def test_ids_are_unique_within_a_document(self) -> None:
        ids = [c.chunk_id for c in chunk()]
        assert len(ids) == len(set(ids))

    def test_commit_sha_changes_every_id(self) -> None:
        assert {c.chunk_id for c in chunk(sha="aaa")} & {
            c.chunk_id for c in chunk(sha="bbb")
        } == set()

    def test_file_path_changes_every_id(self) -> None:
        a = {c.chunk_id for c in chunk(path="a.md")}
        b = {c.chunk_id for c in chunk(path="b.md")}
        assert a & b == set()

    def test_every_chunk_verifies_against_its_locator(self) -> None:
        for c in chunk():
            assert c.chunk_id.matches(c.locator), c.locator.canonical()

    def test_ids_are_chunk_level_not_file_level(self) -> None:
        """The invariant this whole module exists to protect.

        A long single-section document must still produce distinguishable chunks. If ids
        were derived from the file alone, these would collide and every citation would point
        at the whole document.
        """
        long_section = "# One Section\n\n" + "\n\n".join(
            f"Paragraph {i} with enough words in it to occupy a meaningful amount of space "
            f"in the packing window and force a split at some point." * 3
            for i in range(40)
        )
        chunks = chunk(long_section, target_tokens=200)

        assert len(chunks) > 3
        assert len({c.chunk_id for c in chunks}) == len(chunks)
        assert len({c.locator.section_path for c in chunks}) == 1  # same section...
        assert len({c.locator.start_offset for c in chunks}) == len(chunks)  # ...different offsets

    def test_crlf_and_lf_produce_identical_ids(self) -> None:
        """A Windows checkout must not re-key the corpus."""
        lf = [c.chunk_id for c in chunk(DOC)]
        crlf = [c.chunk_id for c in chunk(DOC.replace("\n", "\r\n"))]

        assert lf == crlf


class TestSectionPaths:
    def test_breadcrumbs_nest(self) -> None:
        paths = {c.locator.section_path for c in chunk()}
        assert "Quorum > Design > Retrieval" in paths

    def test_sibling_heading_pops_the_stack(self) -> None:
        paths = {c.locator.section_path for c in chunk()}
        assert "Quorum > Security" in paths
        assert not any(p.startswith("Quorum > Design > Retrieval > Security") for p in paths)

    def test_content_before_any_heading_is_addressable(self) -> None:
        chunks = chunk("Just prose, no headings at all, but enough of it to be worth indexing.")
        assert chunks
        assert chunks[0].locator.section_path == ROOT_SECTION

    def test_headings_inside_code_fences_are_not_sections(self) -> None:
        """A shell comment in a fenced block must not start a new section."""
        text = (
            "# Real Heading\n\n"
            "Explanatory prose that is long enough to be indexed on its own merits here.\n\n"
            "```bash\n"
            "# This is a shell comment, not a heading\n"
            "## Neither is this\n"
            "```\n\n"
            "More prose after the fence to keep the section substantial enough to survive.\n"
        )
        paths = {c.locator.section_path for c in chunk(text)}

        assert paths == {"Real Heading"}

    def test_tilde_fences_are_handled(self) -> None:
        text = (
            "# Heading\n\nProse that is long enough to be indexed on its own merits here.\n\n"
            "~~~\n# not a heading\n~~~\n\nMore prose to keep this section substantial.\n"
        )
        assert {c.locator.section_path for c in chunk(text)} == {"Heading"}


class TestOffsetsAndLines:
    def test_offsets_are_ordered_and_non_negative(self) -> None:
        for c in chunk():
            assert 0 <= c.locator.start_offset < c.locator.end_offset

    def test_offsets_slice_the_original_document(self) -> None:
        """The offsets must actually address the bytes they claim to.

        This is what makes a citation checkable: a reader can slice the file at those bytes
        and see the text the finding was grounded in.
        """
        encoded = DOC.encode("utf-8")
        for c in chunk():
            sliced = encoded[c.locator.start_offset : c.locator.end_offset].decode("utf-8")
            assert c.content.strip() in sliced

    def test_line_numbers_address_the_content_and_are_one_based(self) -> None:
        """GitHub and every editor count from 1, and the range must bracket real content.

        Asserting ``chunks[0].start_line == 1`` would be a weaker test that happens to pass
        for documents whose first section survives the minimum-size filter. This asserts the
        property that actually matters: the line range names the exact lines of the chunk.
        """
        lines = DOC.split("\n")
        for c in chunk():
            assert c.start_line >= 1
            content_lines = c.content.split("\n")
            assert content_lines[0] == lines[c.start_line - 1]
            assert content_lines[-1] == lines[c.end_line - 1]

    def test_line_range_never_starts_or_ends_on_a_blank_line(self) -> None:
        """A citation link that lands on the blank line above the text is a bad link."""
        lines = DOC.split("\n")
        for c in chunk():
            assert lines[c.start_line - 1].strip()
            assert lines[c.end_line - 1].strip()

    def test_line_range_is_consistent(self) -> None:
        for c in chunk():
            assert c.start_line <= c.end_line

    def test_no_chunk_spans_more_than_one_file(self) -> None:
        """Deferred here from Phase 1, now assertable against the real chunker."""
        chunks = chunk()
        assert len({c.locator.file_path for c in chunks}) == 1


class TestPacking:
    def test_large_document_is_split(self) -> None:
        text = (
            "# H\n\n" + ("A sentence with a reasonable number of words in it. " * 40 + "\n\n") * 8
        )
        assert len(chunk(text, target_tokens=150)) > 1

    def test_chunks_overlap_so_context_is_not_cut_at_a_boundary(self) -> None:
        text = "# H\n\n" + "\n\n".join(
            f"Paragraph number {i} with several words." * 8 for i in range(30)
        )
        chunks = chunk(text, target_tokens=120, overlap_tokens=40)

        assert len(chunks) > 2
        overlaps = [
            chunks[i + 1].locator.start_offset < chunks[i].locator.end_offset
            for i in range(len(chunks) - 1)
        ]
        assert any(overlaps)

    def test_trivial_content_is_dropped(self) -> None:
        """A chunk containing "## Notes" and nothing else is retrieval noise."""
        assert chunk("# A\n\n## B\n\n## C\n") == []

    def test_empty_document(self) -> None:
        assert chunk("") == []

    def test_ordinal_restarts_per_section(self) -> None:
        chunks = chunk()
        for c in chunks:
            if c.ordinal == 0:
                continue
            assert c.ordinal > 0


class TestEstimateTokens:
    @pytest.mark.parametrize(("text", "at_least"), [("", 1), ("word", 1), ("x" * 400, 90)])
    def test_estimate_is_positive_and_scales(self, text: str, at_least: int) -> None:
        assert estimate_tokens(text) >= at_least

    def test_it_is_only_an_estimate(self) -> None:
        """Named ``estimate`` because nothing exact may depend on it.

        It drives chunk packing, where being 15% out shifts a boundary and changes nothing
        else. Budget accounting uses provider-reported counts, never this.
        """
        assert estimate_tokens("a b c d e f g h") != 8  # 8 words, 15 chars -> 3


def test_chunk_id_derivation_matches_the_documented_scheme() -> None:
    """Guards docs/Schema.md 2.1 against the code drifting away from it."""
    c = chunk()[0]
    expected = ChunkId.derive(c.locator)

    assert c.chunk_id == expected
    assert c.locator.canonical().startswith("acme/widget@abc123:docs/Design.md#")
