"""Trajectory eval metrics, golden-set loading, and the regression gate.

The gate tests matter most, same reason as ``eval/retrieval``'s: a gate that cannot fail is
worse than no gate.
"""

from __future__ import annotations

import json

import pytest

from eval.trajectory.gate import EmptyBaselineError, check_regression
from eval.trajectory.goldenset import load_goldenset
from eval.trajectory.metrics import (
    LocatedItem,
    finding_precision_recall,
    routing_precision_recall,
)


class TestFindingMatching:
    def test_same_file_same_line_hits(self) -> None:
        p, r = finding_precision_recall([LocatedItem("a.py", 10)], [LocatedItem("a.py", 10)])
        assert p == 1.0
        assert r == 1.0

    def test_same_file_within_window_hits(self) -> None:
        p, r = finding_precision_recall([LocatedItem("a.py", 15)], [LocatedItem("a.py", 10)])
        assert p == 1.0
        assert r == 1.0

    def test_same_file_outside_window_misses(self) -> None:
        p, r = finding_precision_recall([LocatedItem("a.py", 100)], [LocatedItem("a.py", 10)])
        assert p == 0.0
        assert r == 0.0

    def test_different_file_never_hits(self) -> None:
        p, r = finding_precision_recall([LocatedItem("a.py", 10)], [LocatedItem("b.py", 10)])
        assert p == 0.0
        assert r == 0.0

    def test_no_line_on_either_side_matches_on_file_alone(self) -> None:
        p, r = finding_precision_recall([LocatedItem("a.py", None)], [LocatedItem("a.py", 10)])
        assert p == 1.0
        assert r == 1.0

    def test_both_empty_is_a_perfect_score(self) -> None:
        assert finding_precision_recall([], []) == (1.0, 1.0)

    def test_findings_with_no_human_comments_scores_zero_precision(self) -> None:
        """A finding on a PR with no substantive human comments cannot be validated as a hit,
        and reporting it as a perfect score would hide false positives entirely."""
        p, r = finding_precision_recall([LocatedItem("a.py", 10)], [])
        assert p == 0.0
        assert r == 1.0  # vacuously: there is nothing to recall

    def test_human_comments_with_no_findings_scores_zero_recall(self) -> None:
        p, r = finding_precision_recall([], [LocatedItem("a.py", 10)])
        assert p == 1.0  # vacuously: nothing was surfaced, so nothing was wrong
        assert r == 0.0

    def test_extra_finding_drags_down_precision_without_touching_recall(self) -> None:
        p, r = finding_precision_recall(
            [LocatedItem("a.py", 10), LocatedItem("z.py", 999)], [LocatedItem("a.py", 10)]
        )
        assert p == 0.5
        assert r == 1.0


class TestRoutingMatching:
    def test_exact_match_is_perfect(self) -> None:
        p, r = routing_precision_recall(["correctness", "security"], ["correctness", "security"])
        assert (p, r) == (1.0, 1.0)

    def test_missing_expected_specialist_hurts_recall_not_precision(self) -> None:
        p, r = routing_precision_recall(["correctness", "security"], ["correctness"])
        assert p == 1.0
        assert r == 0.5

    def test_extra_specialist_hurts_precision_not_recall(self) -> None:
        p, r = routing_precision_recall(["correctness"], ["correctness", "test_coverage"])
        assert p == 0.5
        assert r == 1.0


class TestGoldensetLoading:
    def test_empty_directory_returns_empty_list(self, tmp_path: object) -> None:
        assert load_goldenset(tmp_path) == []  # type: ignore[arg-type]

    def test_round_trips_a_fixture(self, tmp_path: object) -> None:
        from pathlib import Path

        directory = Path(str(tmp_path))
        fixture = directory / "acme-widget-42.json"
        fixture.write_text(
            json.dumps(
                {
                    "repo": "acme/widget",
                    "pr_number": 42,
                    "base_sha": "abc",
                    "head_sha": "def",
                    "diff": "diff --git a/x b/x\n",
                    "human_comments": [{"file_path": "x.py", "line": 3, "body": "off by one"}],
                    "expected_specialists": ["correctness"],
                }
            ),
            encoding="utf-8",
        )
        cases = load_goldenset(directory)
        assert len(cases) == 1
        assert cases[0].case_id == "acme-widget-42"
        assert cases[0].repo == "acme/widget"
        assert cases[0].human_comments[0].line == 3


class TestGate:
    def _report(self, cases: int, **metrics: float) -> dict[str, object]:
        return {"cases": cases, **metrics}

    def test_no_regression_passes(self) -> None:
        baseline = self._report(
            5, routing_precision=0.8, routing_recall=0.8, finding_precision=0.5, finding_recall=0.5
        )
        current = self._report(
            5, routing_precision=0.8, routing_recall=0.8, finding_precision=0.5, finding_recall=0.5
        )
        result = check_regression(baseline, current)
        assert result.passed
        assert result.compared == 4

    def test_a_real_drop_is_caught(self) -> None:
        """The gate must be provably able to fail -- a gate that cannot fail is worse than none."""
        baseline = self._report(
            5, routing_precision=0.8, routing_recall=0.8, finding_precision=0.5, finding_recall=0.5
        )
        current = self._report(
            5, routing_precision=0.8, routing_recall=0.8, finding_precision=0.1, finding_recall=0.5
        )
        result = check_regression(baseline, current)
        assert not result.passed
        assert result.regressions[0].metric == "finding_precision"

    def test_within_tolerance_passes(self) -> None:
        baseline = self._report(
            5, routing_precision=0.8, routing_recall=0.8, finding_precision=0.5, finding_recall=0.5
        )
        current = self._report(
            5, routing_precision=0.8, routing_recall=0.8, finding_precision=0.47, finding_recall=0.5
        )
        assert check_regression(baseline, current).passed

    def test_empty_baseline_refuses_to_gate(self) -> None:
        with pytest.raises(EmptyBaselineError):
            check_regression(self._report(0), self._report(5, routing_precision=1.0))

    def test_empty_current_run_refuses_to_gate(self) -> None:
        with pytest.raises(EmptyBaselineError):
            check_regression(self._report(5, routing_precision=1.0), self._report(0))

    def test_missing_metric_in_baseline_raises(self) -> None:
        with pytest.raises(ValueError, match="missing from baseline"):
            check_regression(self._report(5), self._report(5, routing_precision=1.0))
