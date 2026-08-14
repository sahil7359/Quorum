"""Trajectory eval metrics.

Human review comments are attached to a file and (usually) a line; a finding is attached to a
file and, optionally, a line range. Matching them is a **location-proximity heuristic, not a
semantic judge** -- a finding "hits" a human comment when it names the same file within
``LINE_WINDOW`` lines. This will both undercount (a finding correctly identifies the real issue
but cites a different line than the human did) and overcount (two unrelated issues on adjacent
lines coincide). Stated here so the number is not mistaken for ground truth, the same reason
``eval/retrieval/goldenset.py`` states its own label bias up front.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

LINE_WINDOW = 10
"""How close a finding's line must be to a human comment's line to count as the same issue.
Wide enough that an off-by-a-few-lines citation (common when a hunk shifts) still matches;
narrow enough that a finding on an unrelated function far away does not."""


@dataclass(frozen=True, slots=True)
class LocatedItem:
    file_path: str
    line: int | None


def _hits(a: LocatedItem, b: LocatedItem) -> bool:
    if a.file_path != b.file_path:
        return False
    if a.line is None or b.line is None:
        return True
    return abs(a.line - b.line) <= LINE_WINDOW


def finding_precision_recall(
    findings: Sequence[LocatedItem], human_comments: Sequence[LocatedItem]
) -> tuple[float, float]:
    """Precision: fraction of findings that land near a real human comment. Vacuously 1.0
    when nothing was surfaced -- an empty review makes no false claims.

    Recall: fraction of human comments that some finding landed near. Vacuously 1.0 when
    there were no human comments to find -- nothing was there to miss. The two vacuous cases
    are independent (surfacing findings on a PR with no human comments scores precision 0.0
    even though recall is vacuously 1.0), which is why each is guarded on its own list rather
    than on "both empty".
    """
    precision = (
        1.0
        if not findings
        else sum(1 for f in findings if any(_hits(f, c) for c in human_comments)) / len(findings)
    )
    recall = (
        1.0
        if not human_comments
        else sum(1 for c in human_comments if any(_hits(f, c) for f in findings))
        / len(human_comments)
    )
    return precision, recall


def routing_precision_recall(expected: Sequence[str], actual: Sequence[str]) -> tuple[float, float]:
    """Precision/recall of the chosen specialist set against the hand-labelled expectation."""
    expected_set, actual_set = set(expected), set(actual)
    if not expected_set and not actual_set:
        return 1.0, 1.0
    precision = len(expected_set & actual_set) / len(actual_set) if actual_set else 0.0
    recall = len(expected_set & actual_set) / len(expected_set) if expected_set else 0.0
    return precision, recall


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0
