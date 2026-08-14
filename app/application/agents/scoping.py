"""AST-scoped context.

A specialist reviewing a three-line change does not need the whole 800-line file. It needs
the *enclosing definitions* of the changed lines — the function that changed, plus the class
header that gives it meaning — and nothing else.

Why this matters here specifically: the binding constraint is 100K tokens per day. A review
carrying four whole files costs several times one carrying four scoped regions, and the
difference is the difference between two reviews a day and four.

Falls back to a fixed window around changed lines when the file will not parse — which
includes every non-Python file, and Python files that are mid-edit in the diff. **The
fallback is the common case for a polyglot repository and is not a failure path.**
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass

from app.domain.entities import ChangedFile
from app.domain.text import estimate_tokens

FALLBACK_CONTEXT_LINES = 12


@dataclass(frozen=True, slots=True)
class ScopedRegion:
    """A contiguous span of a file that a specialist should actually read."""

    file_path: str
    start_line: int
    end_line: int
    content: str
    strategy: str
    """``ast`` when enclosing definitions were resolved, ``window`` when we fell back."""

    @property
    def token_estimate(self) -> int:
        return estimate_tokens(self.content)


@dataclass(frozen=True, slots=True)
class ScopingReport:
    """What scoping cost and saved. The source of the published reduction figure."""

    regions: tuple[ScopedRegion, ...]
    tokens_whole_file: int
    tokens_scoped: int

    @property
    def reduction_pct(self) -> float:
        if self.tokens_whole_file <= 0:
            return 0.0
        return round(100.0 * (1 - self.tokens_scoped / self.tokens_whole_file), 2)

    @property
    def ast_regions(self) -> int:
        return sum(1 for r in self.regions if r.strategy == "ast")


def changed_line_numbers(changed: ChangedFile) -> set[int]:
    """1-based line numbers in the *new* file touched by this file's hunks.

    Walks each hunk body counting only lines that exist in the new file (added and context),
    because a removed line has no position in the new file to scope around.
    """
    touched: set[int] = set()
    for hunk in changed.hunks:
        cursor = hunk.new_start
        for line in hunk.content.split("\n"):
            if line.startswith("-"):
                continue
            if line.startswith("+"):
                touched.add(cursor)
            cursor += 1
    return touched


def _enclosing_spans(source: str, targets: set[int]) -> list[tuple[int, int]]:
    """Smallest top-level definition spans containing any target line."""
    tree = ast.parse(source)
    spans: list[tuple[int, int]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        start = min([node.lineno, *[d.lineno for d in node.decorator_list]])
        end = node.end_lineno or node.lineno
        if any(start <= line <= end for line in targets):
            spans.append((start, end))

    if not spans:
        return []

    # why: keep only the *smallest* span covering each target. ast.walk yields the enclosing
    #      class as well as the changed method, and including both would ship the entire
    #      class -- defeating the point of scoping on exactly the files that need it most.
    #      alt: keep every matching span (simpler, and often larger than the whole file)
    minimal: list[tuple[int, int]] = []
    for span in spans:
        if not any(
            other != span and span[0] <= other[0] and other[1] <= span[1] for other in spans
        ):
            minimal.append(span)
    return _merge(sorted(set(minimal)))


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def scope_file(changed: ChangedFile, source: str) -> list[ScopedRegion]:
    """Reduce one file to the regions a reviewer needs."""
    targets = changed_line_numbers(changed)
    if not targets:
        return []

    lines = source.replace("\r\n", "\n").split("\n")
    spans: list[tuple[int, int]] = []
    strategy = "window"

    if changed.file_path.endswith(".py"):
        try:
            spans = _enclosing_spans(source, targets)
            if spans:
                strategy = "ast"
        except SyntaxError:
            # Expected, not exceptional: a file can be syntactically invalid at this commit.
            spans = []

    if not spans:
        spans = _merge(
            sorted(
                (
                    max(1, line - FALLBACK_CONTEXT_LINES),
                    min(len(lines), line + FALLBACK_CONTEXT_LINES),
                )
                for line in targets
            )
        )

    return [
        ScopedRegion(
            file_path=changed.file_path,
            start_line=start,
            end_line=end,
            content="\n".join(lines[start - 1 : end]),
            strategy=strategy,
        )
        for start, end in spans
    ]


def scope_diff(files: Sequence[tuple[ChangedFile, str]]) -> ScopingReport:
    """Scope every changed file and report what it saved against a whole-file baseline."""
    regions: list[ScopedRegion] = []
    whole = 0
    for changed, source in files:
        whole += estimate_tokens(source)
        regions.extend(scope_file(changed, source))

    return ScopingReport(
        regions=tuple(regions),
        tokens_whole_file=whole,
        tokens_scoped=sum(r.token_estimate for r in regions),
    )
