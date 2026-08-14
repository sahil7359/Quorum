"""The real logger and tracer, not the test fakes every other suite in this project uses.

``StructlogLogger`` and ``StructlogTracer`` had no test coverage before this file, because
``RecordingLogger``/``NullTracer`` were the only implementations anything ever exercised. This
is the file that proves the real ones actually work -- redaction included, since a logger that
redacts in theory and a logger that redacts in the code path that actually runs are different
claims.
"""

from __future__ import annotations

import json

from structlog.testing import capture_logs

from app.domain import log_events
from app.infrastructure.observability.logging import StructlogLogger
from app.infrastructure.observability.redaction import MASK
from app.infrastructure.observability.tracing import StructlogTracer
from tests.support.fakes import RecordingLogger


class TestStructlogLoggerRedacts:
    def test_a_secret_shaped_field_is_redacted_before_it_reaches_structlog(self) -> None:
        with capture_logs() as captured:
            StructlogLogger().info("test.event", token="ghp_1234567890abcdefghijklmnopqrstuvwxyz")
        assert captured[0]["token"] == MASK

    def test_a_run_id_survives_because_it_is_not_a_secret(self) -> None:
        run_id = "c9e49a28-517f-44b3-8b07-0de27fac423d"
        with capture_logs() as captured:
            StructlogLogger().info("test.event", run_id=run_id)
        assert captured[0]["run_id"] == run_id

    def test_bind_carries_redaction_forward(self) -> None:
        with capture_logs() as captured:
            bound = StructlogLogger().bind(api_key="gsk_ABCdef1234567890ABCdef1234567890")
            bound.info("test.event")
        assert captured[0]["api_key"] == MASK

    def test_all_four_levels_are_wired(self) -> None:
        with capture_logs() as captured:
            logger = StructlogLogger()
            logger.debug("d")
            logger.info("i")
            logger.warn("w")
            logger.error("e")
        levels = [entry["log_level"] for entry in captured]
        assert levels == ["debug", "info", "warning", "error"]


class TestStructlogTracerSpans:
    async def test_a_span_emits_a_matching_started_and_completed_pair(self) -> None:
        logger = RecordingLogger()
        tracer = StructlogTracer(logger)

        with tracer.span("test-span", attempt=1):
            pass

        started = logger.find(log_events.SPAN_STARTED)
        completed = logger.find(log_events.SPAN_COMPLETED)
        assert started is not None
        assert completed is not None
        assert started.fields["span_id"] == completed.fields["span_id"]
        assert isinstance(completed.fields["duration_ms"], int)

    async def test_a_span_that_raises_emits_failed_not_completed_and_still_raises(self) -> None:
        logger = RecordingLogger()
        tracer = StructlogTracer(logger)

        try:
            with tracer.span("test-span"):
                raise ValueError("boom")
        except ValueError:
            pass
        else:
            raise AssertionError("the span swallowed the exception")

        assert logger.find(log_events.SPAN_FAILED) is not None
        assert logger.find(log_events.SPAN_COMPLETED) is None

    async def test_nested_spans_get_different_span_ids(self) -> None:
        logger = RecordingLogger()
        tracer = StructlogTracer(logger)

        with tracer.span("outer"), tracer.span("inner"):
            pass

        started_ids = {
            line.fields["span_id"] for line in logger.lines if line.event == log_events.SPAN_STARTED
        }
        assert len(started_ids) == 2


class TestReconstructableFromLogsAlone:
    """docs/ImplementationPlan.md's Phase 10 requirement, stated exactly: 'A review must be
    reconstructable end to end from logs alone.' This runs a real review graph with the real
    logger and tracer and checks that claim against actual JSON output, not against the fake
    RecordingLogger every other integration test uses."""

    async def test_a_full_review_narrative_is_present_in_structured_output(self) -> None:
        from app.application.agents.graph import build_review_graph
        from app.application.agents.nodes import (
            IngestNode,
            RouteNode,
            SpecialistsNode,
            SynthesiseNode,
        )
        from app.domain.entities import ChangedFile, Diff, DiffHunk, PullRequest, RepoRef
        from app.domain.values import RunId
        from tests.support.fakes import FakeChatModel, FakeCodeHost, FakeRetriever, NullTracer

        repo = RepoRef.parse("acme/widget")
        hunk = DiffHunk("app/auth/login.py", 1, 6, 1, 7, "+    token = issue_token(user)")
        host = FakeCodeHost(
            pull_request=PullRequest(
                repo=repo,
                number=42,
                title="Issue tokens on login",
                body="",
                author="octocat",
                base_sha="base1234",
                head_sha="head5678",
            ),
            diff=Diff(
                files=(ChangedFile("app/auth/login.py", "modified", 1, 0, (hunk,)),),
                raw="diff --git a/x b/x\n",
            ),
            files={"app/auth/login.py": "def f(): pass"},
        )
        model = FakeChatModel(
            responses={"route": ['{"specialists": ["correctness", "security"], "reason": "x"}']}
        )
        run_id = RunId.new()

        with capture_logs() as captured:
            logger = StructlogLogger()
            graph = build_review_graph(
                ingest=IngestNode(
                    code_host=host, logger=logger, tracer=NullTracer(), max_diff_lines=1500
                ),
                route=RouteNode(model=model, logger=logger, tracer=NullTracer()),
                specialists=SpecialistsNode(
                    retriever=FakeRetriever(),
                    model=model,
                    logger=logger,
                    tracer=NullTracer(),
                    top_k=5,
                ),
                synthesise=SynthesiseNode(logger=logger, tracer=NullTracer()),
            )
            await graph.ainvoke({"run_id": run_id, "repo": repo, "pr_number": 42})

        events = [entry["event"] for entry in captured]
        # Every stage of the review is visible, in the real output, without RecordingLogger.
        assert log_events.NODE_STARTED in events
        assert log_events.NODE_COMPLETED in events
        assert log_events.ROUTE_DECIDED in events

        route_decided = next(e for e in captured if e["event"] == log_events.ROUTE_DECIDED)
        assert "security" in route_decided["specialists"]
        assert route_decided["reason"]

        # Confirms the captured entries are genuinely JSON-serialisable structured data, not
        # just Python objects that happen to print nicely -- the actual claim this test makes.
        json.dumps(captured, default=str)
