"""The retrieval regression gate.

Split deliberately into two pieces:

* :func:`check_regression` — pure comparison logic, unit-tested in milliseconds and
  provably able to fail.
* :func:`main` — runs the real eval and applies the gate. Slow, run in CI and on demand.

The split exists because a gate whose logic is only exercised by a 30-second end-to-end run
is a gate nobody proves. My previous project shipped a CI gate that asserted a tautology and
passed while scoring 0.0; the defence against repeating that is comparison logic I can break
on purpose in a unit test.

Compares against **committed** baselines (``eval/baselines/retrieval.json``). A gate that
compares a run against itself is a tautology, not a gate.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = REPO_ROOT / "eval" / "baselines" / "retrieval.json"

TOLERANCE = 0.02
"""How far a metric may fall below baseline before the gate fails.

Non-zero because embedding models are not bit-deterministic across ONNX runtime versions and
hardware. 0.02 on NDCG@5 is roughly one query in twenty moving one rank -- small enough to
catch a real regression, large enough not to fail on noise.
"""

GATED_METRICS = ("ndcg_at_5", "recall_at_5", "success_at_5")
GATED_CONFIGS = ("dense", "bm25", "hybrid")
"""``hybrid+rerank`` is deliberately not gated: it is disabled by default (ADR-0004) and
kept only so the comparison stays reproducible. Gating a configuration we do not ship would
fail CI for a change that affects nothing."""


class CorpusMismatchError(ValueError):
    """Baseline and current run measured different corpora, so they are incomparable."""


@dataclass(frozen=True)
class Regression:
    config: str
    metric: str
    baseline: float
    current: float

    @property
    def delta(self) -> float:
        return self.current - self.baseline

    def __str__(self) -> str:
        return (
            f"{self.config}.{self.metric}: {self.current:.4f} vs baseline "
            f"{self.baseline:.4f} ({self.delta:+.4f})"
        )


@dataclass(frozen=True)
class GateResult:
    regressions: tuple[Regression, ...]
    compared: int

    @property
    def passed(self) -> bool:
        return not self.regressions


def _index(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["config"]: row for row in report.get("results", [])}


def check_regression(
    baseline: dict[str, Any],
    current: dict[str, Any],
    *,
    tolerance: float = TOLERANCE,
    configs: tuple[str, ...] = GATED_CONFIGS,
    metrics: tuple[str, ...] = GATED_METRICS,
) -> GateResult:
    """Fail if any gated metric fell more than ``tolerance`` below baseline.

    Raises rather than passing when a gated configuration is missing from either report.
    A gate that silently compares nothing is the failure mode this whole module exists to
    avoid -- "0 comparisons, PASS" must be impossible.
    """
    baseline_sha = baseline.get("corpus_sha")
    current_sha = current.get("corpus_sha")
    if baseline_sha and current_sha and baseline_sha != current_sha:
        # why: the corpus is this repo's own docs/, so adding an ADR changes retrieval
        #      results. Reporting that as a regression is misleading -- the retriever is
        #      unchanged. Raising forces an explicit re-baseline instead of a false alarm.
        #      alt: compare anyway (fails CI every time documentation is written)
        raise CorpusMismatchError(
            f"baseline was measured on corpus {baseline_sha}, this run used {current_sha}. "
            "Re-baseline with: uv run python -m eval.retrieval.runner --write-baseline"
        )

    baseline_rows = _index(baseline)
    current_rows = _index(current)

    regressions: list[Regression] = []
    compared = 0

    for config in configs:
        if config not in baseline_rows:
            raise ValueError(f"config {config!r} missing from baseline -- cannot gate on nothing")
        if config not in current_rows:
            raise ValueError(f"config {config!r} missing from current run -- eval did not run")

        for metric in metrics:
            if metric not in baseline_rows[config]:
                raise ValueError(f"metric {metric!r} missing from baseline for {config!r}")
            before = float(baseline_rows[config][metric])
            after = float(current_rows[config][metric])
            compared += 1
            if after < before - tolerance:
                regressions.append(Regression(config, metric, before, after))

    if compared == 0:
        raise ValueError("gate compared 0 metrics; refusing to report a pass")

    return GateResult(regressions=tuple(regressions), compared=compared)


def main() -> int:
    import asyncio
    from dataclasses import asdict

    from eval.retrieval.runner import run

    if not BASELINE_PATH.exists():
        print(f"no baseline at {BASELINE_PATH}; run the runner with --write-baseline first")
        return 2

    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    current = asdict(asyncio.run(run()))

    try:
        result = check_regression(baseline, current)
    except CorpusMismatchError as mismatch:
        print(f"\nSKIP: {mismatch}")
        return 3

    print(f"\ngate: compared {result.compared} metrics against committed baseline")
    if result.passed:
        print("PASS")
        return 0

    print("FAIL")
    for regression in result.regressions:
        print(f"  - {regression}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
