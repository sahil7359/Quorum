"""Graph nodes, and the base class that makes an untraced node impossible to write.

``TracedNode.__call__`` is ``final``: it emits ``node.started`` / ``node.completed`` /
``node.failed`` and opens a tracing span, then delegates to the subclass's ``run``. A node
author cannot forget to instrument, because instrumentation is not something they do.

This is the pattern from DataChat's ``test_every_node_is_traced``, and the reason for it is
that per-node observability decays the moment it is a convention: the fifth node added in a
hurry is the one that is silent, and it is always the one that breaks.
"""

from __future__ import annotations

import contextlib
import time
from abc import ABC, abstractmethod
from typing import ClassVar, final

from app.application.agents import routing, specialists
from app.application.agents.scoping import scope_diff
from app.application.agents.state import ReviewState
from app.domain import log_events
from app.domain.entities import CandidateFinding, Chunk, Diff, RepoRef
from app.domain.errors import AllSpecialistsFailedError, CodeHostError
from app.domain.grounding import deduplicate, ground_candidates
from app.domain.ports import ChatModelPort, CodeHostPort, LoggerPort, RetrieverPort, TracerPort
from app.domain.values import ChunkId, RunStatus, SpecialistKind, TokenUsage


class TracedNode(ABC):
    """Base class for every graph node. Instrumentation lives here, not in subclasses."""

    name: ClassVar[str]

    def __init__(self, *, logger: LoggerPort, tracer: TracerPort) -> None:
        self._logger = logger
        self._tracer = tracer

    @final
    async def __call__(self, state: ReviewState) -> dict[str, object]:
        self._safe_log("info", log_events.NODE_STARTED, node=self.name)
        started = time.perf_counter()
        try:
            with self._tracer.span(self.name):
                result = await self.run(state)
        except Exception as exc:
            self._safe_log(
                "error",
                log_events.NODE_FAILED,
                node=self.name,
                error=f"{type(exc).__name__}: {exc}"[:300],
            )
            raise
        self._safe_log(
            "info",
            log_events.NODE_COMPLETED,
            node=self.name,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        return result

    @abstractmethod
    async def run(self, state: ReviewState) -> dict[str, object]:
        """Do the node's work and return a partial state update."""

    def _safe_log(self, level: str, event: str, **fields: object) -> None:
        # Telemetry never fails a request -- including the telemetry in the base class.
        # Telemetry never fails a request. Suppression here is the policy, not an oversight.
        with contextlib.suppress(Exception):
            getattr(self._logger, level)(event, **fields)


class IngestNode(TracedNode):
    """Fetch the pull request and its diff, cap it, and scope the context."""

    name = "ingest"

    def __init__(
        self,
        *,
        code_host: CodeHostPort,
        logger: LoggerPort,
        tracer: TracerPort,
        max_diff_lines: int,
    ) -> None:
        super().__init__(logger=logger, tracer=tracer)
        self._code_host = code_host
        self._max_diff_lines = max_diff_lines

    async def run(self, state: ReviewState) -> dict[str, object]:
        repo = state["repo"]
        number = state["pr_number"]

        pull_request = await self._code_host.get_pull_request(repo, number)
        diff = await self._code_host.get_diff(repo, number, max_lines=self._max_diff_lines)

        excerpt, reduction = await self._scoped_excerpt(repo, diff, pull_request.head_sha)
        signals = routing.compute_signals(diff)

        return {
            "pull_request": pull_request,
            "commit_sha": pull_request.head_sha,
            "diff": diff,
            "diff_excerpt": excerpt,
            "symbols": list(signals.new_public_symbols),
            "paths": list(diff.touched_paths),
            "scoping_reduction_pct": reduction,
        }

    async def _scoped_excerpt(self, repo: RepoRef, diff: Diff, ref: str) -> tuple[str, float]:
        """Build the code excerpt sent to specialists, scoped to changed regions.

        Falls back to the raw diff when file contents cannot be fetched. That is a *quality*
        degradation, not a failure: the raw diff is still reviewable, it just costs more
        tokens and gives less surrounding context.
        """
        sources: list[tuple[object, str]] = []
        for changed in diff.files:
            try:
                sources.append(
                    (changed, await self._code_host.get_file(repo, changed.file_path, ref=ref))
                )
            except CodeHostError:
                continue

        if not sources:
            return diff.raw, 0.0

        report = scope_diff(sources)  # type: ignore[arg-type]
        if not report.regions:
            return diff.raw, 0.0

        self._safe_log(
            "info",
            log_events.CONTEXT_SCOPED,
            files=len(sources),
            tokens_whole_file=report.tokens_whole_file,
            tokens_scoped=report.tokens_scoped,
            reduction_pct=report.reduction_pct,
            ast_regions=report.ast_regions,
        )
        excerpt = "\n\n".join(
            f"--- {r.file_path}:{r.start_line}-{r.end_line} ({r.strategy}-scoped) ---\n{r.content}"
            for r in report.regions
        )
        return excerpt, report.reduction_pct


class RouteNode(TracedNode):
    """The supervisor. Heuristics set the floor; the model may only extend it."""

    name = "route"

    def __init__(
        self,
        *,
        model: ChatModelPort,
        logger: LoggerPort,
        tracer: TracerPort,
        model_name: str | None = None,
        consult_model: bool = True,
    ) -> None:
        super().__init__(logger=logger, tracer=tracer)
        self._model = model
        self._model_name = model_name
        self._consult_model = consult_model

    async def run(self, state: ReviewState) -> dict[str, object]:
        from app.application.agents.prompts import ROUTING_SCHEMA, build_routing_messages

        diff = state["diff"]
        signals = routing.compute_signals(diff)
        floor, _ = routing.heuristic_floor(signals)

        llm_specialists: list[SpecialistKind] | None = None
        llm_reason = ""
        usage: list[TokenUsage] = []

        if self._consult_model:
            messages = build_routing_messages(
                pr_title=state["pull_request"].title,
                signals_summary=signals.summary(),
                heuristic_floor=floor,
            )
            try:
                completion = await self._model.complete(
                    messages, node="route", model=self._model_name, json_schema=ROUTING_SCHEMA
                )
                usage = [completion.usage]
                parsed = routing.parse_llm_routing(completion.content)
                if parsed is None:
                    self._safe_log(
                        "warn",
                        log_events.ROUTE_LLM_UNPARSEABLE,
                        raw_excerpt=completion.content[:120],
                    )
                else:
                    llm_specialists, llm_reason = list(parsed[0]), parsed[1]
            except Exception as exc:
                self._safe_log(
                    "warn", log_events.ROUTE_LLM_UNPARSEABLE, raw_excerpt=f"provider: {exc}"[:120]
                )

        decision, removal_attempted = routing.decide(
            signals, llm_specialists=llm_specialists, llm_reason=llm_reason
        )

        if removal_attempted:
            self._safe_log(
                "warn",
                log_events.ROUTE_LLM_IGNORED,
                attempted_removal=[k.value for k in removal_attempted],
            )

        # The single most important log line in the system.
        self._safe_log(
            "info",
            log_events.ROUTE_DECIDED,
            specialists=[k.value for k in decision.specialists],
            reason=decision.reason,
            heuristic_floor=[k.value for k in decision.heuristic_floor],
            llm_added=[k.value for k in decision.llm_added],
            signals=signals.summary(),
        )
        return {"routing": decision, "usage": usage}


class SpecialistsNode(TracedNode):
    """Run the chosen specialists and collect their candidate findings."""

    name = "specialists"

    def __init__(
        self,
        *,
        retriever: RetrieverPort,
        model: ChatModelPort,
        logger: LoggerPort,
        tracer: TracerPort,
        top_k: int,
        model_name: str | None = None,
    ) -> None:
        super().__init__(logger=logger, tracer=tracer)
        self._retriever = retriever
        self._model = model
        self._top_k = top_k
        self._model_name = model_name

    async def run(self, state: ReviewState) -> dict[str, object]:
        decision = state["routing"]
        candidates: list[CandidateFinding] = []
        corpus: dict[str, Chunk] = {}
        visible: dict[str, list[str]] = {}
        usage: list[TokenUsage] = []
        failed: list[str] = []

        # why: sequential by default. Groq's free tier is 12K tokens/minute and three
        #      specialists dispatched together trip it. Concurrency is a config knob
        #      (specialist_concurrency) rather than a hardcoded gather, because local Ollama
        #      has no such ceiling and eval runs would otherwise be needlessly slow.
        #      alt: asyncio.gather always (faster locally, rate-limited in production)
        for specialist in decision.specialists:
            result = await specialists.run_specialist(
                specialist,
                run_id=state["run_id"],
                pr_title=state["pull_request"].title,
                diff_excerpt=state["diff_excerpt"],
                symbols=state.get("symbols", []),
                paths=state.get("paths", []),
                repo=state["repo"],
                commit_sha=state["commit_sha"],
                retriever=self._retriever,
                model=self._model,
                logger=self._logger,
                top_k=self._top_k,
                model_name=self._model_name,
            )
            if result.failed:
                failed.append(specialist.value)
            candidates.extend(result.candidates)
            if result.usage is not None:
                usage.append(result.usage)

            # Visibility is recorded per specialist from what retrieval actually returned.
            visible[specialist.value] = [str(scored.chunk_id) for scored in result.offered]
            for scored in result.offered:
                corpus[str(scored.chunk_id)] = scored.chunk

        if len(failed) == len(decision.specialists) and decision.specialists:
            raise AllSpecialistsFailedError(
                "every specialist failed; an empty review is not a clean review"
            )

        return {
            "candidates": candidates,
            "corpus": corpus,
            "visible": visible,
            "failed_specialists": failed,
            "usage": usage,
        }


class SynthesiseNode(TracedNode):
    """Enforce cite-or-drop, deduplicate, and rank.

    Grounding happens **in code**, before the synthesis model is involved at all. The model
    is not asked to check citations; it never sees a candidate that failed grounding. Prompts
    are advisory and code is not.
    """

    name = "synthesise"

    def __init__(self, *, logger: LoggerPort, tracer: TracerPort) -> None:
        super().__init__(logger=logger, tracer=tracer)

    async def run(self, state: ReviewState) -> dict[str, object]:
        # Rebuild value objects at the domain boundary. State carries strings so it can
        # be checkpointed; the domain takes ChunkId and SpecialistKind.
        corpus = {ChunkId(key): chunk for key, chunk in state.get("corpus", {}).items()}
        visible = {
            SpecialistKind(name): [ChunkId(value) for value in ids]
            for name, ids in state.get("visible", {}).items()
        }
        result = ground_candidates(state.get("candidates", []), corpus=corpus, visible=visible)

        for dropped in result.dropped:
            self._safe_log(
                "info",
                log_events.FINDING_DROPPED,
                specialist=dropped.candidate.specialist.value,
                reason=dropped.reason,
            )

        findings = deduplicate(result.findings)
        ranked = tuple(sorted(findings, key=lambda f: f.rank_key, reverse=True))

        for finding in ranked:
            self._safe_log(
                "info",
                log_events.FINDING_RAISED,
                specialist=finding.specialist.value,
                severity=finding.severity.value,
                chunk_id=str(finding.citation.chunk_id),
                confidence=finding.confidence,
            )

        return {
            "findings": list(ranked),
            "dropped": [d.reason for d in result.dropped],
            "status": RunStatus.PROPOSED,
        }
