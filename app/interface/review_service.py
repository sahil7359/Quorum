"""The composition root's request lifecycle. ``docs/AppFlow.md`` §§1-3 and 11.

Ties idempotency, the review cache, the live-review rate limit and the daily token budget
around the read-only review graph (``ingest -> route -> specialists -> synthesise``).

**Publishing is deliberately not wired in here.** The approval gate, durable ``interrupt()``
and ``publish`` node are Phase 5's already-built machinery -- reachable by constructing the
graph with ``approval``/``publish`` nodes and a checkpointer instead of ``None``. But the HTTP
surface for a human to submit approve/reject decisions and resume a checkpointed graph across
requests is its own design surface (thread-id management, SSE reconnection semantics for a
stream that outlives the request that started it) and is out of scope for this pass. See
``learn/08-serving.md`` for why that line was drawn here rather than folded in.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import Any

from app.application.agents.graph import build_review_graph
from app.application.agents.nodes import IngestNode, RouteNode, SpecialistsNode, SynthesiseNode
from app.domain import log_events
from app.domain.entities import RepoRef, Review, RoutingDecision
from app.domain.ports import (
    BudgetPort,
    ChatModelPort,
    ClockPort,
    CodeHostPort,
    LoggerPort,
    RateLimiterPort,
    RetrieverPort,
    ReviewCachePort,
    TracerPort,
)
from app.domain.values import RunId, RunStatus, SpecialistKind
from app.infrastructure.mcp.quorum_server import finding_to_dict
from app.interface.ingestion_service import IngestionService


@dataclass
class ReviewService:
    """One instance per process. Holds no per-request state beyond in-flight idempotency
    coalescing -- everything durable lives behind the ports it was constructed with."""

    code_host: CodeHostPort
    route_model: ChatModelPort
    specialist_model: ChatModelPort
    retriever: RetrieverPort
    cache: ReviewCachePort
    budget: BudgetPort
    rate_limiter: RateLimiterPort
    clock: ClockPort
    logger: LoggerPort
    tracer: TracerPort
    config_hash: str
    max_diff_lines: int
    retrieval_top_k: int
    # why optional: every test-built service uses a retriever that needs no ingestion step
    # (FakeRetriever, or a store pre-populated directly). Only the real composition root,
    # reviewing repos nobody has pre-ingested, needs this -- see ingestion_service.py.
    ingestion: IngestionService | None = None
    _in_flight: dict[str, asyncio.Task[Review]] = field(default_factory=dict, repr=False)

    async def review(
        self, repo: RepoRef, pr_number: int, *, idempotency_key: str | None = None
    ) -> Review:
        """Guardrail G14. An ``Idempotency-Key`` that is already running coalesces onto the
        same in-flight task rather than starting a second one; the cache (below) already
        handles a *repeated* request for something already finished. Coalescing is what the
        cache alone cannot do, because two concurrent requests both miss the cache before
        either has written to it.

        Scope: in-process only. A second process (a redeployed instance, a second replica)
        would not see this dict and could start a duplicate run. Durable, cross-process
        idempotency needs a persisted key, which is future work -- stated plainly rather than
        implied to work, the same caveat Phase 7 recorded for its in-process review dict.
        """
        if idempotency_key is None:
            return await self._run(repo, pr_number)

        existing = self._in_flight.get(idempotency_key)
        if existing is not None:
            return await existing

        task = asyncio.ensure_future(self._run(repo, pr_number))
        self._in_flight[idempotency_key] = task
        try:
            return await task
        finally:
            self._in_flight.pop(idempotency_key, None)

    async def review_stream(
        self, repo: RepoRef, pr_number: int
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """SSE source: ``docs/AppFlow.md``'s promise that the review streams as it forms.

        Yields ``(event_name, payload)`` pairs. Runs the same cache/rate-limit/budget
        preflight as :meth:`review`; a cache hit, a refusal, or a budget-exhausted fallback
        each yield exactly one ``review.completed`` event and return.

        **Scope note.** LangGraph's ``astream(stream_mode="updates")`` yields one update per
        graph *node* -- ``ingest``, ``route``, ``specialists``, ``synthesise`` -- because that
        is the graph's actual topology (``nodes.py``'s ``SpecialistsNode`` loops over all
        three specialists *inside one node*, not as three separate graph steps). So this
        streams "the specialists step finished", not "correctness finished, then security
        started" as ``docs/AppFlow.md``'s prose suggests. Splitting specialists into per-
        specialist graph nodes would change a graph topology every Phase 4/5 test already
        depends on; noted as real, deliberately out-of-scope future work rather than silently
        matching prose that does not describe what this streams.

        No idempotency coalescing here: two callers cannot meaningfully share one async
        generator's yields the way they share one awaited ``Task``. A concurrent duplicate
        streamed request will genuinely run twice. Documented, not hidden -- the same posture
        as every other scope line drawn in this file.
        """
        run_id = RunId.new()
        pull_request = await self.code_host.get_pull_request(repo, pr_number)
        cache_key = self._cache_key(repo, pr_number, pull_request.head_sha)

        cached = await self.cache.get(cache_key)
        if cached is not None:
            self.logger.info(log_events.CACHE_HIT, cache_key=cache_key)
            yield "review.completed", review_event(cached)
            return
        self.logger.info(log_events.CACHE_MISS, cache_key=cache_key)

        if not await self.rate_limiter.try_acquire():
            self.logger.warn(log_events.RATE_LIMITED, repo=str(repo), pr_number=pr_number)
            refused = self._refused(
                run_id,
                repo,
                pr_number,
                pull_request.head_sha,
                "live review rate limit reached for today; try again tomorrow or check the "
                "cache for an existing review",
            )
            yield "review.completed", review_event(refused)
            return

        budget_state = await self.budget.state()
        if budget_state.exhausted:
            self.logger.warn(
                log_events.BUDGET_EXHAUSTED,
                consumed=budget_state.consumed,
                limit=budget_state.limit,
            )
            fallback = await self.cache.get_latest(repo, pr_number)
            if fallback is not None:
                banner = (
                    f"daily token budget exhausted; showing a cached review from "
                    f"{fallback.finished_at.isoformat() if fallback.finished_at else 'an earlier run'}"
                )
                yield "review.completed", review_event(_with_banner(fallback, banner))
                return
            refused = self._refused(
                run_id,
                repo,
                pr_number,
                pull_request.head_sha,
                "daily token budget exhausted; no cached review is available for this pull request",
            )
            yield "review.completed", review_event(refused)
            return

        if self.ingestion is not None:
            # why an event straddles this call rather than just awaiting it: on a repo
            # nobody has ingested yet, this can be the slowest single step in the whole
            # request -- and it is the *only* step with nothing to yield mid-way, unlike the
            # graph nodes below. A caller (or a reverse proxy sitting in front of one) sees a
            # completely silent connection for however long ingestion takes, with none of the
            # graph's own SSE traffic to prove the request is still alive. Two events instead
            # of zero costs nothing and gives a proxy something to see before its own idle
            # timeout decides the connection is dead.
            yield "ingestion.started", {"repo": str(repo), "commit_sha": pull_request.head_sha}
            chunks_ingested = await self.ingestion.ensure_ingested(repo, pull_request.head_sha)
            yield "ingestion.completed", {"chunks": chunks_ingested}

        graph = build_review_graph(
            ingest=IngestNode(
                code_host=self.code_host,
                logger=self.logger,
                tracer=self.tracer,
                max_diff_lines=self.max_diff_lines,
            ),
            route=RouteNode(model=self.route_model, logger=self.logger, tracer=self.tracer),
            specialists=SpecialistsNode(
                retriever=self.retriever,
                model=self.specialist_model,
                logger=self.logger,
                tracer=self.tracer,
                top_k=self.retrieval_top_k,
            ),
            synthesise=SynthesiseNode(logger=self.logger, tracer=self.tracer),
        )

        self.logger.info(log_events.RUN_STARTED, run_id=str(run_id), repo=str(repo), pr=pr_number)
        started = self.clock.now()
        accumulated: dict[str, Any] = {}
        async for update in graph.astream(
            {
                "run_id": run_id,
                "repo": repo,
                "pr_number": pr_number,
                "commit_sha": pull_request.head_sha,
            },
            stream_mode="updates",
        ):
            for node_name, partial in update.items():
                accumulated.update(partial)
                yield f"node.{node_name}", _node_event(node_name, partial)
        finished = self.clock.now()

        for usage in accumulated.get("usage", []):
            await self.budget.record(run_id, usage)

        review = Review(
            run_id=run_id,
            repo=repo,
            pr_number=pr_number,
            head_sha=pull_request.head_sha,
            status=RunStatus.PROPOSED,
            routing=accumulated["routing"],
            findings=tuple(accumulated.get("findings", [])),
            diff_truncated=accumulated["diff"].truncated,
            started_at=started,
            finished_at=finished,
        )
        await self.cache.put(cache_key, review)
        self.logger.info(
            log_events.RUN_COMPLETED, run_id=str(run_id), findings=len(review.findings)
        )
        yield "review.completed", review_event(review)

    async def _run(self, repo: RepoRef, pr_number: int) -> Review:
        run_id = RunId.new()
        pull_request = await self.code_host.get_pull_request(repo, pr_number)
        cache_key = self._cache_key(repo, pr_number, pull_request.head_sha)

        cached = await self.cache.get(cache_key)
        if cached is not None:
            self.logger.info(log_events.CACHE_HIT, cache_key=cache_key)
            return cached
        self.logger.info(log_events.CACHE_MISS, cache_key=cache_key)

        if not await self.rate_limiter.try_acquire():
            self.logger.warn(log_events.RATE_LIMITED, repo=str(repo), pr_number=pr_number)
            return self._refused(
                run_id,
                repo,
                pr_number,
                pull_request.head_sha,
                "live review rate limit reached for today; try again tomorrow or check the "
                "cache for an existing review",
            )

        budget_state = await self.budget.state()
        if budget_state.exhausted:
            self.logger.warn(
                log_events.BUDGET_EXHAUSTED,
                consumed=budget_state.consumed,
                limit=budget_state.limit,
            )
            fallback = await self.cache.get_latest(repo, pr_number)
            if fallback is not None:
                return _with_banner(
                    fallback,
                    f"daily token budget exhausted; showing a cached review from "
                    f"{fallback.finished_at.isoformat() if fallback.finished_at else 'an earlier run'}",
                )
            return self._refused(
                run_id,
                repo,
                pr_number,
                pull_request.head_sha,
                "daily token budget exhausted; no cached review is available for this pull request",
            )

        if self.ingestion is not None:
            await self.ingestion.ensure_ingested(repo, pull_request.head_sha)

        graph = build_review_graph(
            ingest=IngestNode(
                code_host=self.code_host,
                logger=self.logger,
                tracer=self.tracer,
                max_diff_lines=self.max_diff_lines,
            ),
            route=RouteNode(model=self.route_model, logger=self.logger, tracer=self.tracer),
            specialists=SpecialistsNode(
                retriever=self.retriever,
                model=self.specialist_model,
                logger=self.logger,
                tracer=self.tracer,
                top_k=self.retrieval_top_k,
            ),
            synthesise=SynthesiseNode(logger=self.logger, tracer=self.tracer),
        )

        self.logger.info(log_events.RUN_STARTED, run_id=str(run_id), repo=str(repo), pr=pr_number)
        started = self.clock.now()
        try:
            result = await graph.ainvoke(
                {
                    "run_id": run_id,
                    "repo": repo,
                    "pr_number": pr_number,
                    "commit_sha": pull_request.head_sha,
                }
            )
        except Exception as exc:
            self.logger.error(
                log_events.RUN_FAILED,
                run_id=str(run_id),
                error=f"{type(exc).__name__}: {exc}"[:300],
            )
            raise
        finished = self.clock.now()

        for usage in result.get("usage", []):
            await self.budget.record(run_id, usage)

        review = Review(
            run_id=run_id,
            repo=repo,
            pr_number=pr_number,
            head_sha=pull_request.head_sha,
            status=RunStatus.PROPOSED,
            routing=result["routing"],
            findings=tuple(result.get("findings", [])),
            diff_truncated=result["diff"].truncated,
            started_at=started,
            finished_at=finished,
        )
        await self.cache.put(cache_key, review)
        self.logger.info(
            log_events.RUN_COMPLETED, run_id=str(run_id), findings=len(review.findings)
        )
        return review

    def _cache_key(self, repo: RepoRef, pr_number: int, head_sha: str) -> str:
        material = f"{repo}:{pr_number}:{head_sha}:{self.config_hash}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _refused(
        self, run_id: RunId, repo: RepoRef, pr_number: int, head_sha: str, message: str
    ) -> Review:
        # why: Review.routing is not Optional -- a review that never routed still needs a
        #      RoutingDecision to construct one, and RoutingDecision itself requires
        #      `correctness` and a non-empty reason (Phase 1's invariant). Rather than fight
        #      that by loosening a domain type for one caller, a refusal states in its own
        #      reason that nothing actually ran.
        routing = RoutingDecision(
            specialists=(SpecialistKind.CORRECTNESS,),
            reason=f"refused before routing: {message}",
        )
        return Review(
            run_id=run_id,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            status=RunStatus.FAILED,
            routing=routing,
            findings=(),
            error=message,
        )


def _with_banner(review: Review, banner: str) -> Review:
    return replace(review, error=banner)


def review_event(review: Review) -> dict[str, Any]:
    return {
        "run_id": str(review.run_id),
        "repo": str(review.repo),
        "pr_number": review.pr_number,
        "head_sha": review.head_sha,
        "status": review.status.value,
        "findings": [finding_to_dict(f) for f in review.ranked_findings],
        "diff_truncated": review.diff_truncated,
        "error": review.error,
        # Stated in every event, not only in documentation -- docs/MCP.md's ReviewRecord
        # makes the same promise for the MCP surface, and a client of either should not have
        # to read a README to learn that Quorum did not touch their repository.
        "posted_to_github": False,
    }


def _node_event(node_name: str, partial: dict[str, Any]) -> dict[str, Any]:
    """A JSON-safe summary of one graph node's output. Deliberately not the raw state --
    ``partial`` can carry domain objects (``RoutingDecision``, a ``Diff``) that are not
    themselves JSON-serialisable, and a specialist's raw ``CandidateFinding``s are untrusted
    model output that has not passed cite-or-drop yet and should not reach a client looking
    like a finding."""
    if node_name == "ingest":
        diff = partial.get("diff")
        return {
            "files_changed": len(diff.files) if diff is not None else 0,
            "diff_truncated": diff.truncated if diff is not None else False,
            "scoping_reduction_pct": partial.get("scoping_reduction_pct", 0.0),
        }
    if node_name == "route":
        routing = partial.get("routing")
        if routing is None:
            return {}
        return {
            "specialists": [k.value for k in routing.specialists],
            "reason": routing.reason,
            "heuristic_floor": [k.value for k in routing.heuristic_floor],
            "llm_added": [k.value for k in routing.llm_added],
        }
    if node_name == "specialists":
        return {
            "candidates_proposed": len(partial.get("candidates", [])),
            "failed_specialists": partial.get("failed_specialists", []),
        }
    if node_name == "synthesise":
        return {
            "findings": [finding_to_dict(f) for f in partial.get("findings", [])],
            "dropped": partial.get("dropped", []),
        }
    return {}
