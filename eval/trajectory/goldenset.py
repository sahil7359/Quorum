"""The trajectory eval golden set.

Unlike ``eval/retrieval``'s golden set, these labels are not mine: each fixture is a real
merged pull request carrying real human review comments, so recall is measured against an
**imperfect but independent ceiling** -- human reviewers miss things too. That framing has to
survive into any write-up of these numbers; see ``learn/06``.

Fixtures live as JSON files in ``eval/trajectory/goldenset/``, one per PR, named
``<owner>-<repo>-<pr>.json``. None are committed yet -- this run had no ``QUORUM_GITHUB_TOKEN``.
Assemble them with ``uv run python -m eval.trajectory.fetch_fixtures`` once one exists.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

GOLDENSET_DIR = Path(__file__).resolve().parent / "goldenset"


@dataclass(frozen=True, slots=True)
class HumanComment:
    """One real review comment, used as a label -- never fed to the model."""

    file_path: str
    line: int | None
    body: str
    author: str = ""


@dataclass(frozen=True, slots=True)
class DocFile:
    """One documentation file from the reviewed repo, at the PR's base commit."""

    file_path: str
    content: str


@dataclass(frozen=True, slots=True)
class TrajectoryCase:
    """One golden-set case: a real merged PR, plus the labels it is scored against."""

    case_id: str
    repo: str
    pr_number: int
    url: str
    title: str
    body: str
    author: str
    base_sha: str
    head_sha: str
    diff: str
    changed_files: dict[str, str] = field(default_factory=dict)
    """path -> full file content at head_sha, for AST-scoped context (mirrors what
    ``IngestNode`` fetches from a real code host)."""
    doc_corpus: tuple[DocFile, ...] = ()
    human_comments: tuple[HumanComment, ...] = ()
    expected_specialists: tuple[str, ...] = ()
    """Hand-labelled: which specialists this PR actually warranted. Mine, not the model's --
    grading routing against a label I wrote is the same trade-off ``eval/retrieval`` made,
    kept only for routing, not for findings."""
    note: str = ""


def load_case(path: Path) -> TrajectoryCase:
    data = json.loads(path.read_text(encoding="utf-8"))
    return TrajectoryCase(
        case_id=path.stem,
        repo=data["repo"],
        pr_number=int(data["pr_number"]),
        url=data.get("url", ""),
        title=data.get("title", ""),
        body=data.get("body", ""),
        author=data.get("author", ""),
        base_sha=data["base_sha"],
        head_sha=data["head_sha"],
        diff=data["diff"],
        changed_files=data.get("changed_files", {}),
        doc_corpus=tuple(DocFile(**d) for d in data.get("doc_corpus", [])),
        human_comments=tuple(HumanComment(**c) for c in data.get("human_comments", [])),
        expected_specialists=tuple(data.get("expected_specialists", [])),
        note=data.get("note", ""),
    )


def load_goldenset(directory: Path = GOLDENSET_DIR) -> list[TrajectoryCase]:
    """Every fixture in the golden-set directory, sorted for reproducible ordering.

    Returns an empty list rather than raising when no fixtures exist yet. An empty golden set
    is a fact about this run's data collection, not an error the harness should hide --
    ``eval.trajectory.runner`` reports it as ``TODO: not yet run`` rather than pretending.
    """
    if not directory.exists():
        return []
    return [load_case(p) for p in sorted(directory.glob("*.json"))]
