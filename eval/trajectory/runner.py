"""Trajectory eval runner.

Runs the real review graph -- ingest, route, specialists, synthesise, with **no write path**
(``approval=None, publish=None``) -- against real merged pull requests carrying real human
review comments, and scores routing and finding location against those comments.

**Recall here is measured against an imperfect ceiling.** Human reviewers miss things; a
finding a human reviewer did not make is not necessarily wrong. Read the numbers as "how much
of what a human reviewer actually said would a caller relying on this system have seen", not
as "how complete is this review". See ``eval/trajectory/metrics.py`` for the matching
heuristic and ``learn/06`` for the write-up.

Run: ``uv run python -m eval.trajectory.runner``
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from app.application.agents.graph import build_review_graph
from app.application.agents.nodes import IngestNode, RouteNode, SpecialistsNode, SynthesiseNode
from app.domain.entities import PullRequest, RepoRef
from app.domain.values import RunId
from app.infrastructure.llm.ollama import OllamaChatModel
from app.infrastructure.mcp.diff_parser import parse_unified_diff
from app.infrastructure.retrieval.chunker import chunk_markdown
from app.infrastructure.retrieval.dense import FastEmbedEmbedder, InMemoryChunkStore
from app.infrastructure.retrieval.hybrid import HybridRetriever
from eval.trajectory.goldenset import TrajectoryCase, load_goldenset
from eval.trajectory.metrics import (
    LocatedItem,
    finding_precision_recall,
    mean,
    routing_precision_recall,
)
from tests.support.fakes import FakeCodeHost, NullTracer, RecordingLogger

REPO_ROOT = Path(__file__).resolve().parents[2]
OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_MODEL = "llama3.1:8b"
DIFF_LINE_CAP = 10_000
"""Real PRs can be larger than Quorum's production QUORUM_MAX_DIFF_LINES cap. The eval
measures review quality against the whole diff rather than silently truncating a golden-set
PR to fit a production cost control that is a separate, already-measured concern."""


@dataclass
class CaseResult:
    case_id: str
    repo: str
    pr_number: int
    routing_precision: float
    routing_recall: float
    finding_precision: float
    finding_recall: float
    findings_surfaced: int
    findings_dropped: int
    human_comments: int
    model_calls: int
    total_tokens: int
    wall_clock_s: float
    error: str = ""


@dataclass
class TrajectoryReport:
    cases: int
    cases_errored: int
    routing_precision: float
    routing_recall: float
    finding_precision: float
    finding_recall: float
    mean_model_calls: float
    mean_total_tokens: float
    results: list[CaseResult]


async def _run_case(case: TrajectoryCase, *, verbose: bool) -> CaseResult:
    logger = RecordingLogger()
    tracer = NullTracer()
    model = OllamaChatModel(base_url=OLLAMA_BASE_URL, model=OLLAMA_MODEL, logger=logger)

    try:
        diff = parse_unified_diff(case.diff, max_lines=DIFF_LINE_CAP)
        repo_ref = RepoRef.parse(case.repo)
        host = FakeCodeHost(
            pull_request=PullRequest(
                repo=repo_ref,
                number=case.pr_number,
                title=case.title,
                body=case.body,
                author=case.author,
                base_sha=case.base_sha,
                head_sha=case.head_sha,
                url=case.url,
            ),
            diff=diff,
            files=case.changed_files,
        )

        chunks = []
        for doc in case.doc_corpus:
            chunks.extend(
                chunk_markdown(
                    doc.content,
                    repo=case.repo,
                    commit_sha=case.base_sha,
                    file_path=doc.file_path,
                )
            )
        embedder = FastEmbedEmbedder()
        store = InMemoryChunkStore()
        if chunks:
            await store.upsert(chunks, await embedder.embed([c.content for c in chunks]))
        retriever = HybridRetriever(store=store, embedder=embedder, logger=logger, candidates=30)

        graph = build_review_graph(
            ingest=IngestNode(
                code_host=host, logger=logger, tracer=tracer, max_diff_lines=DIFF_LINE_CAP
            ),
            route=RouteNode(model=model, logger=logger, tracer=tracer),
            specialists=SpecialistsNode(
                retriever=retriever, model=model, logger=logger, tracer=tracer, top_k=5
            ),
            synthesise=SynthesiseNode(logger=logger, tracer=tracer),
        )

        started = time.perf_counter()
        result = await graph.ainvoke(
            {
                "run_id": RunId.new(),
                "repo": repo_ref,
                "pr_number": case.pr_number,
                "commit_sha": case.head_sha,
            }
        )
        elapsed = time.perf_counter() - started

        routing = result["routing"]
        findings = result["findings"]
        usage = result.get("usage", [])

        located_findings = [LocatedItem(f.file_path, f.line_start) for f in findings if f.file_path]
        located_comments = [LocatedItem(c.file_path, c.line) for c in case.human_comments]

        finding_p, finding_r = finding_precision_recall(located_findings, located_comments)
        routing_p, routing_r = routing_precision_recall(
            case.expected_specialists, [k.value for k in routing.specialists]
        )

        if verbose:
            print(
                f"  {case.case_id}: routing P={routing_p:.2f} R={routing_r:.2f}  "
                f"finding P={finding_p:.2f} R={finding_r:.2f}  "
                f"{len(findings)} surfaced, {len(usage)} calls, {elapsed:.1f}s"
            )

        return CaseResult(
            case_id=case.case_id,
            repo=case.repo,
            pr_number=case.pr_number,
            routing_precision=round(routing_p, 4),
            routing_recall=round(routing_r, 4),
            finding_precision=round(finding_p, 4),
            finding_recall=round(finding_r, 4),
            findings_surfaced=len(findings),
            findings_dropped=len(result.get("dropped", [])),
            human_comments=len(case.human_comments),
            model_calls=len(usage),
            total_tokens=sum(u.total_tokens for u in usage),
            wall_clock_s=round(elapsed, 2),
        )
    except Exception as exc:
        if verbose:
            print(f"  {case.case_id}: ERROR {exc}")
        return CaseResult(
            case_id=case.case_id,
            repo=case.repo,
            pr_number=case.pr_number,
            routing_precision=0.0,
            routing_recall=0.0,
            finding_precision=0.0,
            finding_recall=0.0,
            findings_surfaced=0,
            findings_dropped=0,
            human_comments=len(case.human_comments),
            model_calls=0,
            total_tokens=0,
            wall_clock_s=0.0,
            error=str(exc),
        )


def _empty_report() -> TrajectoryReport:
    return TrajectoryReport(
        cases=0,
        cases_errored=0,
        routing_precision=0.0,
        routing_recall=0.0,
        finding_precision=0.0,
        finding_recall=0.0,
        mean_model_calls=0.0,
        mean_total_tokens=0.0,
        results=[],
    )


async def run(*, verbose: bool = True) -> TrajectoryReport:
    cases = load_goldenset()
    if not cases:
        if verbose:
            print(
                "no fixtures in eval/trajectory/goldenset/ -- TODO: not yet run.\n"
                "Assemble some with: uv run python -m eval.trajectory.fetch_fixtures "
                "(needs QUORUM_GITHUB_TOKEN)."
            )
        return _empty_report()

    results = [await _run_case(case, verbose=verbose) for case in cases]
    ok = [r for r in results if not r.error]

    report = TrajectoryReport(
        cases=len(cases),
        cases_errored=len(results) - len(ok),
        routing_precision=round(mean([r.routing_precision for r in ok]), 4),
        routing_recall=round(mean([r.routing_recall for r in ok]), 4),
        finding_precision=round(mean([r.finding_precision for r in ok]), 4),
        finding_recall=round(mean([r.finding_recall for r in ok]), 4),
        mean_model_calls=round(mean([float(r.model_calls) for r in ok]), 2),
        mean_total_tokens=round(mean([float(r.total_tokens) for r in ok]), 1),
        results=results,
    )

    if verbose:
        print(
            f"\n{report.cases} cases ({report.cases_errored} errored)\n"
            f"routing  P={report.routing_precision:.4f} R={report.routing_recall:.4f}\n"
            f"finding  P={report.finding_precision:.4f} R={report.finding_recall:.4f}\n"
            f"cost     {report.mean_model_calls:.1f} calls/case, "
            f"{report.mean_total_tokens:.0f} tokens/case"
        )

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Quorum trajectory eval")
    parser.add_argument("--write-baseline", action="store_true")
    args = parser.parse_args()

    report = asyncio.run(run())

    runs_dir = REPO_ROOT / "eval" / "_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "trajectory-latest.json").write_text(
        json.dumps(asdict(report), indent=2), encoding="utf-8"
    )

    if args.write_baseline:
        if report.cases == 0:
            raise SystemExit("refusing to write a baseline from 0 cases")
        baseline = REPO_ROOT / "eval" / "baselines" / "trajectory.json"
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")
        print(f"\nbaseline written to {baseline.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
