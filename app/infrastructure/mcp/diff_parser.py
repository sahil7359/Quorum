"""Unified diff -> domain entities.

Kept separate from the MCP client because it is pure text processing with no I/O, which
means it can be tested exhaustively against awkward diffs without a server anywhere.

Truncation is handled here rather than at the call site, and it is *recorded* rather than
silent: a review that saw only the first 1,500 lines but does not say so is a lie about its
own coverage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.domain.entities import ChangedFile, Diff, DiffHunk

_DIFF_GIT = re.compile(r"^diff --git a/(?P<old>.+?) b/(?P<new>.+)$")
_HUNK = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_lines>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_lines>\d+))? @@"
)
_NEW_FILE = re.compile(r"^new file mode ")
_DELETED_FILE = re.compile(r"^deleted file mode ")


@dataclass(slots=True)
class _FileAccumulator:
    path: str
    status: str = "modified"
    additions: int = 0
    deletions: int = 0
    hunks: list[DiffHunk] = field(default_factory=list)


def parse_unified_diff(raw: str, *, max_lines: int) -> Diff:
    """Parse a unified diff, truncating at ``max_lines`` and saying so if it did.

    Args:
        raw: the unified diff as returned by the code host.
        max_lines: hard cap. Guardrail G8.
    """
    all_lines = raw.splitlines()
    truncated = len(all_lines) > max_lines
    lines = all_lines[:max_lines] if truncated else all_lines

    files: list[_FileAccumulator] = []
    current: _FileAccumulator | None = None
    hunk_header: re.Match[str] | None = None
    hunk_body: list[str] = []

    def close_hunk() -> None:
        nonlocal hunk_header, hunk_body
        if current is not None and hunk_header is not None:
            current.hunks.append(
                DiffHunk(
                    file_path=current.path,
                    old_start=int(hunk_header["old_start"]),
                    old_lines=int(hunk_header["old_lines"] or 1),
                    new_start=int(hunk_header["new_start"]),
                    new_lines=int(hunk_header["new_lines"] or 1),
                    content="\n".join(hunk_body),
                )
            )
        hunk_header = None
        hunk_body = []

    for line in lines:
        header = _DIFF_GIT.match(line)
        if header:
            close_hunk()
            current = _FileAccumulator(path=header["new"])
            files.append(current)
            continue

        if current is None:
            continue

        if _NEW_FILE.match(line):
            current.status = "added"
            continue
        if _DELETED_FILE.match(line):
            current.status = "removed"
            continue

        hunk = _HUNK.match(line)
        if hunk:
            close_hunk()
            hunk_header = hunk
            continue

        if hunk_header is None:
            continue

        hunk_body.append(line)
        # why: inside a hunk, a leading +/- is unambiguously an added/removed line. The
        #      "+++ b/path" file headers sit *before* the first @@ and are already skipped
        #      by the hunk_header guard above, so they never reach this counter.
        #      I originally excluded lines starting with "+++" here as well. That guard was
        #      both dead (headers cannot arrive) and wrong: an added line whose content
        #      begins with "++" arrives as "+++..." and would have been silently uncounted.
        #      Found by deliberately breaking it and watching the tests stay green.
        #      alt: re-add the "+++" exclusion (looks safer, undercounts real content)
        if line.startswith("+"):
            current.additions += 1
        elif line.startswith("-"):
            current.deletions += 1

    close_hunk()

    return Diff(
        files=tuple(
            ChangedFile(
                file_path=f.path,
                status=f.status,
                additions=f.additions,
                deletions=f.deletions,
                hunks=tuple(f.hunks),
            )
            for f in files
        ),
        raw="\n".join(lines),
        truncated=truncated,
        truncated_at_line=max_lines if truncated else None,
    )
