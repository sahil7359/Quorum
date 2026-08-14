"""Measure what AST context scoping actually saves.

Uses **real diffs from this repository's own git history** rather than synthetic fixtures,
because the figure is published and a synthetic diff would measure my fixture-writing.

For each commit it compares:

* **whole-file baseline** — every changed file, in full, as a naive reviewer would send it
* **scoped** — only the enclosing definitions of the changed lines

Run: ``uv run python -m eval.scoping.runner``
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from app.application.agents.scoping import scope_diff
from app.infrastructure.mcp.diff_parser import parse_unified_diff

REPO_ROOT = Path(__file__).resolve().parents[2]
MAX_COMMITS = 25


def _git(*args: str) -> str:
    # why: explicit utf-8 with replacement. Git output on Windows decodes with the console
    #      codepage by default and dies on any non-ASCII byte in a diff -- and this repo's
    #      own history is full of em dashes.
    #      alt: text=True and hope every commit is ASCII (it is not)
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return result.stdout or "" if result.returncode == 0 else ""


@dataclass
class CommitResult:
    sha: str
    files: int
    ast_regions: int
    tokens_whole_file: int
    tokens_scoped: int
    reduction_pct: float


@dataclass
class ScopingReport:
    commits_measured: int
    files_measured: int
    ast_regions: int
    window_regions: int
    total_tokens_whole_file: int
    total_tokens_scoped: int
    overall_reduction_pct: float
    median_commit_reduction_pct: float
    python_tokens_whole_file: int
    python_tokens_scoped: int
    python_reduction_pct: float
    per_commit: list[CommitResult]


def measure() -> ScopingReport:
    shas = [s for s in _git("log", "--format=%H", f"-{MAX_COMMITS}").split("\n") if s]
    results: list[CommitResult] = []
    total_whole = 0
    total_scoped = 0
    ast_regions = 0
    window_regions = 0
    files_measured = 0
    py_whole = 0
    py_scoped = 0

    for sha in shas:
        raw = _git("show", sha, "--format=", "--unified=3")
        if not raw.strip():
            continue

        diff = parse_unified_diff(raw, max_lines=100_000)
        sources: list[tuple[object, str]] = []
        for changed in diff.files:
            # The file as it exists *at that commit*, which is what a reviewer would read.
            content = _git("show", f"{sha}:{changed.file_path}")
            if content:
                sources.append((changed, content))

        if not sources:
            continue

        report = scope_diff(sources)  # type: ignore[arg-type]
        if not report.regions or report.tokens_whole_file == 0:
            continue

        # why: reported separately because this repository's history is unusually
        #      documentation-heavy, and markdown always takes the window fallback. The
        #      all-files figure therefore understates what AST scoping does on the code
        #      Quorum actually reviews. Both are published; neither is hidden.
        #      alt: report only the flattering Python figure (cherry-picking)
        python_sources = [(c, src) for c, src in sources if c.file_path.endswith(".py")]  # type: ignore[attr-defined]
        if python_sources:
            py = scope_diff(python_sources)  # type: ignore[arg-type]
            py_whole += py.tokens_whole_file
            py_scoped += py.tokens_scoped

        ast_regions += report.ast_regions
        window_regions += len(report.regions) - report.ast_regions
        total_whole += report.tokens_whole_file
        total_scoped += report.tokens_scoped
        files_measured += len(sources)

        results.append(
            CommitResult(
                sha=sha[:10],
                files=len(sources),
                ast_regions=report.ast_regions,
                tokens_whole_file=report.tokens_whole_file,
                tokens_scoped=report.tokens_scoped,
                reduction_pct=report.reduction_pct,
            )
        )

    per_commit = sorted(r.reduction_pct for r in results)
    median = per_commit[len(per_commit) // 2] if per_commit else 0.0

    return ScopingReport(
        commits_measured=len(results),
        files_measured=files_measured,
        ast_regions=ast_regions,
        window_regions=window_regions,
        total_tokens_whole_file=total_whole,
        total_tokens_scoped=total_scoped,
        # why: the overall figure is token-weighted (total saved / total baseline), which is
        #      the number that actually maps to the daily budget. The median per-commit figure
        #      is reported alongside it because one enormous file would otherwise dominate.
        #      alt: report only the mean of per-commit percentages (flattering, less useful)
        overall_reduction_pct=round(100.0 * (1 - total_scoped / total_whole), 2)
        if total_whole
        else 0.0,
        median_commit_reduction_pct=round(median, 2),
        python_tokens_whole_file=py_whole,
        python_tokens_scoped=py_scoped,
        python_reduction_pct=round(100.0 * (1 - py_scoped / py_whole), 2) if py_whole else 0.0,
        per_commit=results,
    )


def main() -> None:
    report = measure()
    if report.commits_measured == 0:
        print("no measurable commits found (is this a git checkout with history?)")
        return

    print(f"commits measured : {report.commits_measured}")
    print(f"files measured   : {report.files_measured}")
    print(f"AST-scoped regions: {report.ast_regions}   window-fallback: {report.window_regions}")
    print(f"whole-file tokens : {report.total_tokens_whole_file:,}")
    print(f"scoped tokens     : {report.total_tokens_scoped:,}")
    print(f"\noverall reduction (token-weighted): {report.overall_reduction_pct:.2f}%")
    print(f"median per-commit reduction        : {report.median_commit_reduction_pct:.2f}%")
    print(
        f"Python files only                  : {report.python_reduction_pct:.2f}% "
        f"({report.python_tokens_whole_file:,} -> {report.python_tokens_scoped:,} tokens)"
    )

    out = REPO_ROOT / "eval" / "baselines" / "scoping.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")
    print(f"\nwritten to {out.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
