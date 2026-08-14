"""Markdown chunking with stable, chunk-level identity.

This is the module that cannot be got wrong later. Every citation Quorum publishes, and
every retrieval number, rests on chunks being addressable at *chunk* level and traceable to
``(file, section, byte offsets)``. File-level ids would silently degrade every citation to
"somewhere in this document" while every test still passed.

Three properties matter, in this order:

1. **Chunk-level identity.** Two chunks from the same file and the same section get
   different ids, because the byte offsets participate in the hash.
2. **Stability.** Re-ingesting the same commit produces byte-identical ids, or the review
   cache is worthless and citations rot between runs.
3. **Traceability.** Every chunk carries the locator its id derives from, so a citation
   renders as a real file and a real line range a human can open.

Offsets are byte offsets into the UTF-8 encoding of the document *after newline
normalisation to ``\\n``*. Normalising first matters on Windows: the same file checked out
with CRLF would otherwise produce different offsets, and therefore different chunk ids, for
identical content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from app.domain.entities import Chunk
from app.domain.values import ChunkLocator

_ATX_HEADING = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(?P<ticks>`{3,}|~{3,})")

ROOT_SECTION: Final[str] = "(document root)"
"""Section path for content appearing before the first heading.

Named rather than left empty so a citation to front-matter reads as something deliberate
instead of a missing value.
"""


@dataclass(frozen=True, slots=True)
class ChunkerConfig:
    """Packing parameters.

    ``target_tokens`` is a soft ceiling: a single paragraph longer than the target is emitted
    whole rather than split mid-sentence, because a chunk cut through the middle of a
    sentence retrieves badly and cites worse.
    """

    target_tokens: int = 320
    overlap_tokens: int = 48
    min_tokens: int = 16


def estimate_tokens(text: str) -> int:
    """Approximate token count at roughly four characters per token.

    Deliberately named *estimate*. It is not a real tokenizer, and nothing that must be
    exact may depend on it -- it drives chunk packing, where being 15% out changes chunk
    boundaries slightly and changes nothing else. Budget accounting uses provider-reported
    counts, never this.
    """
    return max(1, len(text) // 4)


@dataclass(frozen=True, slots=True)
class _Section:
    path: str
    heading_level: int
    start_line: int
    end_line: int


def _split_sections(lines: list[str]) -> list[_Section]:
    """Walk the document, tracking a heading stack, ignoring headings inside code fences."""
    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []
    current_path = ROOT_SECTION
    current_level = 0
    section_start = 0
    fence: str | None = None

    for index, line in enumerate(lines):
        fence_match = _FENCE.match(line)
        if fence_match:
            ticks = fence_match["ticks"]
            # why: a "# Heading" inside a fenced code block is code, not structure. Without
            #      fence tracking, every shell comment in a README starts a new section and
            #      the breadcrumbs become nonsense.
            #      alt: regex headings only at column 0 (still wrong for indented fences)
            if fence is None:
                fence = ticks[0]
            elif ticks[0] == fence:
                fence = None
            continue

        if fence is not None:
            continue

        heading = _ATX_HEADING.match(line)
        if not heading:
            continue

        if index > section_start:
            sections.append(
                _Section(
                    path=current_path,
                    heading_level=current_level,
                    start_line=section_start,
                    end_line=index - 1,
                )
            )

        level = len(heading["hashes"])
        title = heading["title"].strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))

        current_path = " > ".join(part for _, part in stack)
        current_level = level
        section_start = index

    sections.append(
        _Section(
            path=current_path,
            heading_level=current_level,
            start_line=section_start,
            end_line=len(lines) - 1,
        )
    )
    return [s for s in sections if s.end_line >= s.start_line]


def _line_byte_offsets(lines: list[str]) -> list[int]:
    offsets: list[int] = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line.encode("utf-8")) + 1  # +1 for the normalised "\n"
    offsets.append(position)
    return offsets


def _pack(lines: list[str], start: int, end: int, config: ChunkerConfig) -> list[tuple[int, int]]:
    """Group a section's lines into (start_line, end_line) windows on paragraph boundaries."""
    windows: list[tuple[int, int]] = []
    block_start = start
    tokens = 0

    for index in range(start, end + 1):
        tokens += estimate_tokens(lines[index])
        at_paragraph_break = not lines[index].strip()

        if tokens >= config.target_tokens and at_paragraph_break and index > block_start:
            windows.append((block_start, index))
            overlap = 0
            back = index
            while back > block_start and overlap < config.overlap_tokens:
                back -= 1
                overlap += estimate_tokens(lines[back])
            block_start = back
            tokens = overlap

    if block_start <= end:
        windows.append((block_start, end))
    return windows


def chunk_markdown(
    text: str,
    *,
    repo: str,
    commit_sha: str,
    file_path: str,
    config: ChunkerConfig | None = None,
) -> list[Chunk]:
    """Split a markdown document into chunks carrying full, verifiable locators."""
    config = config or ChunkerConfig()
    # Normalise newlines before anything else: CRLF vs LF must not change chunk ids.
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalised.split("\n")
    offsets = _line_byte_offsets(lines)

    chunks: list[Chunk] = []
    for section in _split_sections(lines):
        ordinal = 0
        for raw_start, raw_end in _pack(lines, section.start_line, section.end_line, config):
            # why: packing lands on paragraph breaks, so a window routinely begins or ends on
            #      a blank line. Trimming those before computing offsets keeps start_line and
            #      end_line pointing at real content -- otherwise a citation's line link lands
            #      on an empty line above the text it is citing.
            #      alt: keep the raw window and strip only the content string (offsets and
            #      line numbers then disagree with the text they claim to address)
            window_start, window_end = raw_start, raw_end
            while window_start <= window_end and not lines[window_start].strip():
                window_start += 1
            while window_end >= window_start and not lines[window_end].strip():
                window_end -= 1
            if window_start > window_end:
                continue

            content = "\n".join(lines[window_start : window_end + 1])
            if estimate_tokens(content) < config.min_tokens:
                continue

            locator = ChunkLocator(
                repo=repo,
                commit_sha=commit_sha,
                file_path=file_path,
                section_path=section.path,
                start_offset=offsets[window_start],
                end_offset=offsets[window_end + 1],
            )
            chunks.append(
                Chunk.create(
                    locator=locator,
                    content=content,
                    start_line=window_start + 1,  # 1-based, to match how editors and GitHub count
                    end_line=window_end + 1,
                    ordinal=ordinal,
                    token_count=estimate_tokens(content),
                    heading_level=section.heading_level,
                )
            )
            ordinal += 1

    return chunks
