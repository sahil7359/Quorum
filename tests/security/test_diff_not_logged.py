"""Guardrail G10: diff content never reaches a log line, at any level.

Checked by running the real graph on a diff carrying a distinctive marker string, then
scanning every field of every captured log line for it -- not by trusting a docstring that
claims this, and not by unit-testing a helper function in isolation. An earlier version of
this codebase had exactly that: a function named ``excerpt_for_log`` whose docstring made this
claim, with no caller and no test. It was dead code asserting a property nothing verified.
Deleted; this test verifies the actual, real behaviour of the actual, real log calls instead.
"""

from __future__ import annotations

from app.application.agents.graph import build_review_graph
from app.application.agents.nodes import IngestNode, RouteNode, SpecialistsNode, SynthesiseNode
from app.domain.entities import ChangedFile, Diff, DiffHunk, PullRequest, RepoRef
from app.domain.values import RunId
from tests.support.fakes import (
    FakeChatModel,
    FakeCodeHost,
    FakeRetriever,
    NullTracer,
    RecordingLogger,
)

REPO = RepoRef.parse("acme/widget")
SECRET_MARKER = "QUORUM_TEST_MARKER_sk_live_51H8vGkJz3xQpN9wR"
"""A string shaped like a real secret so this test also stands in for 'a leaked-looking
token in the diff must not be logged either', not just 'diff content in general'."""


def a_diff_carrying_the_marker() -> Diff:
    hunk = DiffHunk(
        "app/auth/login.py",
        1,
        6,
        1,
        7,
        f"+    token = issue_token(user)\n+    api_key = '{SECRET_MARKER}'  # do not log this line",
    )
    return Diff(
        files=(ChangedFile("app/auth/login.py", "modified", 2, 0, (hunk,)),),
        raw=f"diff --git a/app/auth/login.py b/app/auth/login.py\n+{SECRET_MARKER}\n",
    )


async def run_graph_and_collect_logs() -> RecordingLogger:
    host = FakeCodeHost(
        pull_request=PullRequest(
            repo=REPO,
            number=42,
            title="Issue tokens on login",
            body="",
            author="octocat",
            base_sha="base1234",
            head_sha="head5678",
        ),
        diff=a_diff_carrying_the_marker(),
        files={"app/auth/login.py": f"def f():\n    return '{SECRET_MARKER}'\n"},
    )
    model = FakeChatModel(
        responses={"route": ['{"specialists": ["correctness"], "reason": "auth path"}']}
    )
    logger = RecordingLogger()
    tracer = NullTracer()

    graph = build_review_graph(
        ingest=IngestNode(code_host=host, logger=logger, tracer=tracer, max_diff_lines=1500),
        route=RouteNode(model=model, logger=logger, tracer=tracer),
        specialists=SpecialistsNode(
            retriever=FakeRetriever(), model=model, logger=logger, tracer=tracer, top_k=5
        ),
        synthesise=SynthesiseNode(logger=logger, tracer=tracer),
    )
    await graph.ainvoke({"run_id": RunId.new(), "repo": REPO, "pr_number": 42})
    return logger


class TestDiffContentNeverReachesALogLine:
    async def test_diff_content_never_logged_at_info(self) -> None:
        logger = await run_graph_and_collect_logs()
        info_lines = [line for line in logger.lines if line.level == "INFO"]
        assert info_lines, (
            "the graph should have logged something at INFO to make this a real check"
        )

        for line in info_lines:
            for value in line.fields.values():
                assert SECRET_MARKER not in str(value), (
                    f"diff content leaked into an INFO log line: event={line.event!r} "
                    f"fields={line.fields!r}"
                )

    async def test_diff_content_never_logged_at_any_level(self) -> None:
        """Stricter than G10's own wording (INFO specifically) -- there's no level at which
        logging raw diff content is actually fine, so the stronger claim is the one to hold."""
        logger = await run_graph_and_collect_logs()

        for line in logger.lines:
            for value in line.fields.values():
                assert SECRET_MARKER not in str(value), (
                    f"diff content leaked into a {line.level} log line: event={line.event!r} "
                    f"fields={line.fields!r}"
                )

    async def test_the_check_can_actually_fail(self) -> None:
        """Gate proof: prove this test is sensitive to the thing it claims to catch, not just
        structurally incapable of failing."""
        logger = RecordingLogger()
        logger.info("some.event", diff_excerpt=f"leaked: {SECRET_MARKER}")

        leaked = any(
            SECRET_MARKER in str(value) for line in logger.lines for value in line.fields.values()
        )
        assert leaked, "the marker-scan itself failed to notice an obviously leaked value"
