"""The trajectory regression gate.

Split the same way as ``eval/retrieval/gate.py``: :func:`check_regression` is pure comparison
logic, unit-tested in milliseconds; :func:`main` runs the real (slow, model-calling) eval and
applies it. Compares against a **committed** baseline -- a gate that compares a run against
itself is a tautology, not a gate.

Unlike the retrieval gate, there is no corpus-fingerprint problem here: the golden set is
frozen historical pull requests, not a live document tree that the act of writing about it
can change.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = REPO_ROOT / "eval" / "baselines" / "trajectory.json"

TOLERANCE = 0.05
"""Wider than the retrieval gate's 0.02: an LLM's free-text output is not bit-deterministic
even at temperature 0, and the golden set is small enough that one case flipping moves the
mean noticeably. Tightening this deliberately, with more cases, is future work."""

GATED_METRICS = ("routing_precision", "routing_recall", "finding_precision", "finding_recall")


class EmptyBaselineError(ValueError):
    """The baseline was written from zero cases. Refuse to gate against nothing."""


@dataclass(frozen=True)
class Regression:
    metric: str
    baseline: float
    current: float

    @property
    def delta(self) -> float:
        return self.current - self.baseline

    def __str__(self) -> str:
        return (
            f"{self.metric}: {self.current:.4f} vs baseline {self.baseline:.4f} ({self.delta:+.4f})"
        )


@dataclass(frozen=True)
class GateResult:
    regressions: tuple[Regression, ...]
    compared: int

    @property
    def passed(self) -> bool:
        return not self.regressions


def check_regression(
    baseline: dict[str, Any],
    current: dict[str, Any],
    *,
    tolerance: float = TOLERANCE,
    metrics: tuple[str, ...] = GATED_METRICS,
) -> GateResult:
    if baseline.get("cases", 0) == 0:
        raise EmptyBaselineError("baseline has 0 cases; re-baseline with real fixtures first")
    if current.get("cases", 0) == 0:
        raise EmptyBaselineError("current run has 0 cases; no fixtures were scored")

    regressions: list[Regression] = []
    compared = 0
    for metric in metrics:
        if metric not in baseline:
            raise ValueError(f"metric {metric!r} missing from baseline")
        if metric not in current:
            raise ValueError(f"metric {metric!r} missing from current run -- eval did not run")
        before = float(baseline[metric])
        after = float(current[metric])
        compared += 1
        if after < before - tolerance:
            regressions.append(Regression(metric, before, after))

    if compared == 0:
        raise ValueError("gate compared 0 metrics; refusing to report a pass")

    return GateResult(regressions=tuple(regressions), compared=compared)


def main() -> int:
    import asyncio
    from dataclasses import asdict

    from eval.trajectory.runner import run

    if not BASELINE_PATH.exists():
        print(f"no baseline at {BASELINE_PATH}; run the runner with --write-baseline first")
        return 2

    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    current = asdict(asyncio.run(run()))

    try:
        result = check_regression(baseline, current)
    except EmptyBaselineError as empty:
        print(f"\nSKIP: {empty}")
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
