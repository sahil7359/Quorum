"""Sparse retrieval, fusion, metrics and the eval gate.

The gate tests matter most. A gate that cannot fail is worse than no gate, and the specific
disaster being guarded against is a gate that reports PASS having compared nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.infrastructure.retrieval.fusion import reciprocal_rank_fusion
from app.infrastructure.retrieval.sparse import BM25Index, tokenize
from eval.retrieval.gate import CorpusMismatchError, check_regression
from eval.retrieval.metrics import ndcg_at_k, recall_at_k, success_at_k


class TestCodeAwareTokenizer:
    """The reason BM25 is hand-rolled rather than taken off the shelf."""

    def test_camel_case_is_split_and_the_whole_is_kept(self) -> None:
        tokens = tokenize("RetrievalPort")
        assert "retrievalport" in tokens
        assert "retrieval" in tokens
        assert "port" in tokens

    def test_snake_case_is_split_and_the_whole_is_kept(self) -> None:
        tokens = tokenize("QUORUM_MAX_DIFF_LINES")
        assert "quorum_max_diff_lines" in tokens
        assert {"quorum", "max", "diff", "lines"} <= set(tokens)

    def test_acronym_boundaries(self) -> None:
        """``HTTPServerError`` -> http, server, error, not h/t/t/p."""
        assert {"http", "server", "error"} <= set(tokenize("HTTPServerError"))

    def test_plain_words_are_not_exploded(self) -> None:
        assert tokenize("retrieval") == ["retrieval"]

    def test_exact_identifier_outscores_a_partial_match(self) -> None:
        """Keeping the whole identifier means an exact query still wins."""
        index = BM25Index.build(
            [
                ("exact", "The RetrievalPort protocol defines retrieve()."),
                ("partial", "A port for retrieval of documents from somewhere."),
            ]
        )
        results = index.search("RetrievalPort", limit=2)
        assert results[0][0] == "exact"


class TestBM25:
    def test_empty_index_returns_nothing(self) -> None:
        assert BM25Index.build([]).search("anything", limit=5) == []

    def test_empty_query_returns_nothing(self) -> None:
        assert BM25Index.build([("a", "some text")]).search("", limit=5) == []

    def test_unmatched_query_returns_nothing(self) -> None:
        index = BM25Index.build([("a", "cats and dogs")])
        assert index.search("quantum chromodynamics", limit=5) == []

    def test_idf_is_never_negative(self) -> None:
        """The smoothed BM25 idf goes negative for terms in >half the corpus.

        Left unclamped, a document is *penalised* for containing the query term, which on a
        small domain-specific corpus like ours happens for words such as "chunk".
        """
        index = BM25Index.build([(str(i), "chunk chunk chunk") for i in range(10)])
        assert index._idf("chunk") >= 0.0

    def test_results_are_deterministic(self) -> None:
        docs = [(str(i), f"retrieval document number {i} about chunks") for i in range(20)]
        index = BM25Index.build(docs)
        assert index.search("retrieval chunks", limit=5) == index.search(
            "retrieval chunks", limit=5
        )


class TestReciprocalRankFusion:
    def test_agreement_beats_a_single_top_hit(self) -> None:
        """A document ranked second by both beats one ranked first by only one."""
        fused = reciprocal_rank_fusion([["a", "b"], ["c", "b"]])
        assert fused[0][0] == "b"

    def test_scores_are_never_needed(self) -> None:
        """RRF takes only ranks -- no normalisation between cosine and BM25 scales."""
        fused = reciprocal_rank_fusion([["x", "y", "z"]])
        assert [doc for doc, _ in fused] == ["x", "y", "z"]

    def test_partial_overlap_keeps_everything(self) -> None:
        fused = reciprocal_rank_fusion([["a", "b"], ["c", "d"]])
        assert {doc for doc, _ in fused} == {"a", "b", "c", "d"}

    def test_is_deterministic_under_ties(self) -> None:
        """An eval whose ranking reorders between runs is not a measurement."""
        first = reciprocal_rank_fusion([["a", "b"], ["b", "a"]])
        second = reciprocal_rank_fusion([["a", "b"], ["b", "a"]])
        assert first == second

    def test_limit_truncates(self) -> None:
        assert len(reciprocal_rank_fusion([["a", "b", "c", "d"]], limit=2)) == 2

    def test_empty_input(self) -> None:
        assert reciprocal_rank_fusion([]) == []


class TestMetrics:
    def test_ndcg_perfect_ranking_is_one(self) -> None:
        assert ndcg_at_k(["a", "b", "c"], {"a", "b"}, 5) == pytest.approx(1.0)

    def test_ndcg_rewards_higher_placement(self) -> None:
        top = ndcg_at_k(["a", "x", "y"], {"a"}, 5)
        lower = ndcg_at_k(["x", "y", "a"], {"a"}, 5)
        assert top > lower

    def test_ndcg_is_zero_when_nothing_relevant_is_retrieved(self) -> None:
        assert ndcg_at_k(["x", "y"], {"a"}, 5) == 0.0

    def test_ndcg_caps_the_ideal_at_k(self) -> None:
        """With 12 relevant chunks and k=5, a perfect top-5 must still score 1.0."""
        relevant = {f"r{i}" for i in range(12)}
        retrieved = [f"r{i}" for i in range(5)]
        assert ndcg_at_k(retrieved, relevant, 5) == pytest.approx(1.0)

    def test_recall_normalises_by_reachable_items(self) -> None:
        relevant = {f"r{i}" for i in range(12)}
        assert recall_at_k([f"r{i}" for i in range(5)], relevant, 5) == pytest.approx(1.0)

    def test_success_is_one_if_anything_relevant_appears(self) -> None:
        assert success_at_k(["x", "y", "a"], {"a"}, 5) == 1.0
        assert success_at_k(["x", "y"], {"a"}, 5) == 0.0

    def test_no_relevant_items_scores_zero_rather_than_dividing_by_zero(self) -> None:
        assert ndcg_at_k(["a"], set(), 5) == 0.0
        assert recall_at_k(["a"], set(), 5) == 0.0


def report(**configs: dict[str, float]) -> dict[str, object]:
    return {"results": [{"config": name, **values} for name, values in configs.items()]}


BASE = report(
    dense={"ndcg_at_5": 0.58, "recall_at_5": 0.59, "success_at_5": 0.90},
    bm25={"ndcg_at_5": 0.55, "recall_at_5": 0.64, "success_at_5": 0.85},
    hybrid={"ndcg_at_5": 0.59, "recall_at_5": 0.65, "success_at_5": 0.95},
)


class TestGate:
    def test_identical_run_passes(self) -> None:
        result = check_regression(BASE, BASE)
        assert result.passed
        assert result.compared == 9

    def test_improvement_passes(self) -> None:
        better = report(
            dense={"ndcg_at_5": 0.70, "recall_at_5": 0.70, "success_at_5": 0.95},
            bm25={"ndcg_at_5": 0.65, "recall_at_5": 0.70, "success_at_5": 0.90},
            hybrid={"ndcg_at_5": 0.72, "recall_at_5": 0.75, "success_at_5": 1.00},
        )
        assert check_regression(BASE, better).passed

    def test_regression_beyond_tolerance_fails(self) -> None:
        worse = report(
            dense={"ndcg_at_5": 0.58, "recall_at_5": 0.59, "success_at_5": 0.90},
            bm25={"ndcg_at_5": 0.55, "recall_at_5": 0.64, "success_at_5": 0.85},
            hybrid={"ndcg_at_5": 0.40, "recall_at_5": 0.65, "success_at_5": 0.95},
        )
        result = check_regression(BASE, worse)

        assert not result.passed
        assert result.regressions[0].config == "hybrid"
        assert result.regressions[0].metric == "ndcg_at_5"

    def test_small_drop_within_tolerance_passes(self) -> None:
        """Embedding models are not bit-deterministic across runtime versions."""
        jittered = report(
            dense={"ndcg_at_5": 0.57, "recall_at_5": 0.58, "success_at_5": 0.90},
            bm25={"ndcg_at_5": 0.54, "recall_at_5": 0.63, "success_at_5": 0.85},
            hybrid={"ndcg_at_5": 0.58, "recall_at_5": 0.64, "success_at_5": 0.95},
        )
        assert check_regression(BASE, jittered).passed

    def test_missing_config_in_current_run_raises(self) -> None:
        """The eval failing to produce a config must not read as a pass."""
        partial = report(dense=BASE["results"][0])  # type: ignore[index]
        with pytest.raises(ValueError, match="missing from current run"):
            check_regression(BASE, partial)

    def test_missing_config_in_baseline_raises(self) -> None:
        with pytest.raises(ValueError, match="missing from baseline"):
            check_regression(report(), BASE)

    def test_gate_cannot_report_a_pass_having_compared_nothing(self) -> None:
        """The exact failure mode this module exists to prevent."""
        with pytest.raises(ValueError, match="compared 0 metrics"):
            check_regression(BASE, BASE, configs=(), metrics=())

    def test_a_different_corpus_is_incomparable_not_a_regression(self) -> None:
        """The eval corpus is this repo's own docs/, so writing a document moves the scores.

        Found the hard way: adding `docs/adr/0004-rerank-disabled.md` between a baseline run
        and a gate run produced a -0.025 "regression" in hybrid recall. The retriever had not
        changed at all. Reporting that as a regression is a false alarm that trains you to
        ignore the gate, which is worse than having no gate.
        """
        old = {**BASE, "corpus_sha": "aaaaaaaaaaaaaaaa"}
        new = {**BASE, "corpus_sha": "bbbbbbbbbbbbbbbb"}

        with pytest.raises(CorpusMismatchError, match="Re-baseline"):
            check_regression(old, new)

    def test_same_corpus_still_compares(self) -> None:
        same = {**BASE, "corpus_sha": "aaaaaaaaaaaaaaaa"}
        assert check_regression(same, same).compared == 9

    def test_the_committed_baseline_is_gateable(self) -> None:
        """Guards against the committed baseline drifting out of the gated shape."""
        import json

        from eval.retrieval.gate import BASELINE_PATH

        assert BASELINE_PATH.exists(), "no committed retrieval baseline"
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        result = check_regression(baseline, baseline)

        assert result.passed
        assert result.compared == 9


class TestCorpusFingerprint:
    """Found live: CI (Linux, LF) and a Windows checkout of identical committed content
    produced different fingerprints, because the fingerprint hashed raw bytes -- which
    preserve whatever line ending the checkout happened to produce -- while the corpus loader
    it exists to describe reads text with universal-newline translation. The gate correctly
    reported SKIP rather than a false pass/fail, which is the mechanism working, but it meant
    CI never actually ran the comparison. This is the regression test for the fix."""

    def test_identical_content_with_different_line_endings_hashes_the_same(
        self, tmp_path: Path
    ) -> None:
        from eval.retrieval import runner as runner_module

        original_dir = runner_module.CORPUS_DIR
        try:
            lf_dir = tmp_path / "lf"
            crlf_dir = tmp_path / "crlf"
            lf_dir.mkdir()
            crlf_dir.mkdir()

            content = "# Heading\n\nSome text.\n\nMore text.\n"
            (lf_dir / "doc.md").write_bytes(content.encode("utf-8"))
            (crlf_dir / "doc.md").write_bytes(content.replace("\n", "\r\n").encode("utf-8"))

            runner_module.CORPUS_DIR = lf_dir
            lf_fingerprint = runner_module.corpus_fingerprint()

            runner_module.CORPUS_DIR = crlf_dir
            crlf_fingerprint = runner_module.corpus_fingerprint()

            assert lf_fingerprint == crlf_fingerprint
        finally:
            runner_module.CORPUS_DIR = original_dir

    def test_genuinely_different_content_still_hashes_differently(self, tmp_path: Path) -> None:
        """The fix must not make the fingerprint insensitive to real content changes --
        proves this is still a working gate, not a check that always passes."""
        from eval.retrieval import runner as runner_module

        original_dir = runner_module.CORPUS_DIR
        try:
            runner_module.CORPUS_DIR = tmp_path
            (tmp_path / "doc.md").write_text("original content\n", encoding="utf-8")
            first = runner_module.corpus_fingerprint()

            (tmp_path / "doc.md").write_text("changed content\n", encoding="utf-8")
            second = runner_module.corpus_fingerprint()

            assert first != second
        finally:
            runner_module.CORPUS_DIR = original_dir

    def test_file_order_is_case_sensitive_regardless_of_platform(self, tmp_path: Path) -> None:
        """The second live bug, found after the first fix: identical, byte-identical content
        (after normalising line endings) still produced two different fingerprints between a
        Windows checkout and CI (Linux), because the incremental hash is sensitive to file
        *order*, and `pathlib.Path` compares case-insensitively on Windows and case-sensitively
        on Linux -- 'adr/0001-x.md' sorts before 'AppFlow.md' on Windows, after it on Linux.
        This asserts the fix: order matches a plain, case-sensitive string sort of the
        relative paths, which is what every platform will agree on."""
        from eval.retrieval import runner as runner_module

        (tmp_path / "adr").mkdir()
        (tmp_path / "adr" / "0001-x.md").write_text("adr content\n", encoding="utf-8")
        (tmp_path / "AppFlow.md").write_text("appflow content\n", encoding="utf-8")
        (tmp_path / "Design.md").write_text("design content\n", encoding="utf-8")

        original_dir = runner_module.CORPUS_DIR
        try:
            runner_module.CORPUS_DIR = tmp_path
            ordered = [
                p.relative_to(tmp_path).as_posix() for p in runner_module._sorted_corpus_files()
            ]
        finally:
            runner_module.CORPUS_DIR = original_dir

        # A plain Python string sort is case-sensitive on every platform -- the
        # platform-independent ground truth this function has to agree with everywhere.
        assert ordered == sorted(["adr/0001-x.md", "AppFlow.md", "Design.md"])
