"""The review graph end to end, with fakes at every port.

This is the test that proves the two halves built three phases apart are actually connected:
retrieval records *what each specialist was shown*, and grounding drops anything cited
outside that set. Until this ran, cite-or-drop was a tested function that nothing called.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.application.agents.graph import build_review_graph
from app.application.agents.nodes import IngestNode, RouteNode, SpecialistsNode, SynthesiseNode
from app.domain import log_events
from app.domain.entities import (
    ChangedFile,
    Chunk,
    Diff,
    DiffHunk,
    PullRequest,
    RepoRef,
    ScoredChunk,
)
from app.domain.errors import AllSpecialistsFailedError
from app.domain.values import ChunkLocator, RunId, SpecialistKind
from tests.support.fakes import (
    FakeChatModel,
    FakeCodeHost,
    FakeRetriever,
    NullTracer,
    RecordingLogger,
)

REPO = RepoRef.parse("acme/widget")

SOURCE = """def authenticate(user, password):
    if not user:
        return None
    token = issue_token(user)
    token.expires_at = None
    return token
"""


def make_chunk(offset: int, content: str, *, section: str = "Sessions > Expiry") -> Chunk:
    return Chunk.create(
        locator=ChunkLocator(
            repo="acme/widget",
            commit_sha="head5678",
            file_path="docs/security.md",
            section_path=section,
            start_offset=offset,
            end_offset=offset + 200,
        ),
        content=content,
        start_line=1,
        end_line=5,
        ordinal=0,
        token_count=40,
    )


CHUNK_A = make_chunk(0, "All issued tokens must carry an expiry.")
CHUNK_B = make_chunk(200, "Every public function requires a unit test.", section="Testing")


def scored(chunk: Chunk) -> ScoredChunk:
    return ScoredChunk(chunk=chunk, score=0.9)


def a_diff() -> Diff:
    hunk = DiffHunk(
        "app/auth/login.py", 1, 6, 1, 6, "+    token.expires_at = None\n+    return token"
    )
    return Diff(
        files=(ChangedFile("app/auth/login.py", "modified", 2, 0, (hunk,)),),
        raw="diff --git a/app/auth/login.py b/app/auth/login.py\n",
    )


def findings_json(chunk_id: str, title: str = "Token never expires") -> str:
    return json.dumps(
        {
            "findings": [
                {
                    "title": title,
                    "body": "Tokens must carry an expiry.",
                    "severity": "high",
                    "confidence": 0.9,
                    "chunk_id": chunk_id,
                    "file_path": "app/auth/login.py",
                    "line_start": 5,
                }
            ]
        }
    )


def build(
    *,
    model: FakeChatModel,
    retriever: FakeRetriever,
    logger: RecordingLogger,
    code_host: FakeCodeHost | None = None,
) -> Any:
    host = code_host or FakeCodeHost(
        pull_request=PullRequest(
            repo=REPO,
            number=42,
            title="Issue tokens on login",
            body="",
            author="octocat",
            base_sha="base1234",
            head_sha="head5678",
        ),
        diff=a_diff(),
        files={"app/auth/login.py": SOURCE},
    )
    tracer = NullTracer()
    return build_review_graph(
        ingest=IngestNode(
            code_host=host,  # type: ignore[arg-type]  # fake satisfies CodeHostPort structurally
            logger=logger,
            tracer=tracer,
            max_diff_lines=1500,
        ),
        route=RouteNode(model=model, logger=logger, tracer=tracer),
        specialists=SpecialistsNode(
            retriever=retriever, model=model, logger=logger, tracer=tracer, top_k=5
        ),
        synthesise=SynthesiseNode(logger=logger, tracer=tracer),
    )


async def run_graph(graph: Any, **overrides: object) -> dict[str, Any]:
    state = {"run_id": RunId.new(), "repo": REPO, "pr_number": 42, **overrides}
    result: dict[str, Any] = await graph.ainvoke(state)
    return result


class TestHappyPath:
    async def test_a_grounded_finding_survives(self) -> None:
        model = FakeChatModel(
            responses={
                "route": ['{"specialists": ["correctness", "security"], "reason": "auth path"}'],
                "security": [findings_json(str(CHUNK_A.chunk_id))],
            }
        )
        retriever = FakeRetriever(chunks=[scored(CHUNK_A)])
        logger = RecordingLogger()

        result = await run_graph(build(model=model, retriever=retriever, logger=logger))

        assert len(result["findings"]) == 1
        finding = result["findings"][0]
        assert finding.citation.chunk_id == CHUNK_A.chunk_id
        assert finding.specialist is SpecialistKind.SECURITY

    async def test_the_routing_decision_is_logged_with_its_reason(self) -> None:
        """The single most important log line in the system."""
        model = FakeChatModel(
            responses={"route": ['{"specialists": ["correctness"], "reason": "x"}']}
        )
        logger = RecordingLogger()

        await run_graph(build(model=model, retriever=FakeRetriever(), logger=logger))

        line = logger.find(log_events.ROUTE_DECIDED)
        assert line is not None
        assert line.fields["reason"]
        assert "security" in line.fields["specialists"]  # type: ignore[operator]
        assert line.fields["heuristic_floor"]

    async def test_every_node_emits_start_and_completion(self) -> None:
        logger = RecordingLogger()
        await run_graph(build(model=FakeChatModel(), retriever=FakeRetriever(), logger=logger))

        started = [
            line.fields["node"] for line in logger.lines if line.event == log_events.NODE_STARTED
        ]
        assert started == ["ingest", "route", "specialists", "synthesise"]


class TestCiteOrDropIsWired:
    """The wiring these phases exist to connect."""

    async def test_a_hallucinated_chunk_id_is_dropped(self) -> None:
        model = FakeChatModel(
            responses={
                "route": ['{"specialists": ["correctness"], "reason": "x"}'],
                "correctness": [findings_json("0123456789abcdef")],
            }
        )
        logger = RecordingLogger()
        result = await run_graph(
            build(model=model, retriever=FakeRetriever(chunks=[scored(CHUNK_A)]), logger=logger)
        )

        assert result["findings"] == []
        assert "unknown_chunk_id" in result["dropped"]

    async def test_a_finding_with_no_citation_is_dropped(self) -> None:
        model = FakeChatModel(
            responses={
                "route": ['{"specialists": ["correctness"], "reason": "x"}'],
                "correctness": [
                    json.dumps(
                        {
                            "findings": [
                                {
                                    "title": "Something",
                                    "body": "Ungrounded claim",
                                    "severity": "high",
                                    "confidence": 0.9,
                                }
                            ]
                        }
                    )
                ],
            }
        )
        result = await run_graph(
            build(
                model=model,
                retriever=FakeRetriever(chunks=[scored(CHUNK_A)]),
                logger=RecordingLogger(),
            )
        )

        assert result["findings"] == []
        assert "no_citation" in result["dropped"]

    async def test_a_real_chunk_shown_to_another_specialist_is_dropped(self) -> None:
        """The subtle one, now proven end to end.

        The security specialist cites a chunk that genuinely exists and was genuinely
        retrieved — for the test-coverage specialist. Per-specialist visibility catches it;
        a global "does this id exist?" check would not.
        """
        retriever = FakeRetriever(
            per_specialist={
                "security rules": [scored(CHUNK_A)],
                "testing rules": [scored(CHUNK_B)],
            }
        )
        model = FakeChatModel(
            responses={
                "route": ['{"specialists": ["correctness", "security"], "reason": "x"}'],
                # security cites CHUNK_B, which only test_coverage was shown
                "security": [findings_json(str(CHUNK_B.chunk_id))],
            }
        )
        result = await run_graph(build(model=model, retriever=retriever, logger=RecordingLogger()))

        assert result["findings"] == []
        assert "chunk_not_visible_to_specialist" in result["dropped"]

    async def test_retrieval_returning_nothing_means_silence_not_invention(self) -> None:
        """Silence is the correct failure mode for a grounded reviewer."""
        model = FakeChatModel(
            responses={
                "route": ['{"specialists": ["correctness"], "reason": "x"}'],
                "correctness": [findings_json(str(CHUNK_A.chunk_id))],
            }
        )
        result = await run_graph(
            build(model=model, retriever=FakeRetriever(chunks=[]), logger=RecordingLogger())
        )

        assert result["findings"] == []


class TestFailureHandling:
    async def test_one_malformed_specialist_does_not_fail_the_run(self) -> None:
        model = FakeChatModel(
            responses={
                "route": ['{"specialists": ["correctness", "security"], "reason": "x"}'],
                "correctness": ["this is not json at all"],
                "security": [findings_json(str(CHUNK_A.chunk_id))],
            }
        )
        logger = RecordingLogger()
        result = await run_graph(
            build(model=model, retriever=FakeRetriever(chunks=[scored(CHUNK_A)]), logger=logger)
        )

        assert len(result["findings"]) == 1
        assert "correctness" in result["failed_specialists"]
        assert logger.find(log_events.SPECIALIST_FAILED) is not None

    async def test_all_specialists_failing_fails_the_run(self) -> None:
        """An empty review is not a clean review."""
        model = FakeChatModel(
            responses={"route": ['{"specialists": ["correctness"], "reason": "x"}']},
            fail_on={"correctness", "security", "test_coverage"},
        )
        with pytest.raises(AllSpecialistsFailedError):
            await run_graph(
                build(
                    model=model,
                    retriever=FakeRetriever(chunks=[scored(CHUNK_A)]),
                    logger=RecordingLogger(),
                )
            )

    async def test_unparseable_routing_falls_back_to_the_heuristic_floor(self) -> None:
        model = FakeChatModel(responses={"route": ["not json"]})
        logger = RecordingLogger()

        result = await run_graph(build(model=model, retriever=FakeRetriever(), logger=logger))

        assert SpecialistKind.SECURITY in result["routing"].specialists
        assert logger.find(log_events.ROUTE_LLM_UNPARSEABLE) is not None

    async def test_routing_provider_failure_still_produces_a_review(self) -> None:
        model = FakeChatModel(fail_on={"route"})
        result = await run_graph(
            build(model=model, retriever=FakeRetriever(), logger=RecordingLogger())
        )
        assert result["routing"].specialists


class TestUntrustedContentReachesTheModelFenced:
    async def test_diff_content_is_fenced_in_the_specialist_prompt(self) -> None:
        model = FakeChatModel(
            responses={"route": ['{"specialists": ["correctness"], "reason": "x"}']}
        )
        await run_graph(
            build(
                model=model,
                retriever=FakeRetriever(chunks=[scored(CHUNK_A)]),
                logger=RecordingLogger(),
            )
        )

        prompt = model.prompt_for("correctness")
        assert prompt.count("<<<UNTRUSTED_DATA_BEGIN>>>") == prompt.count(
            "<<<UNTRUSTED_DATA_END>>>"
        )

    async def test_diff_content_never_reaches_a_log_line_at_info(self) -> None:
        """Guardrail G10. A diff is attacker-controlled and may contain a credential."""
        secret = "hunter2SuperSecretValue"
        host = FakeCodeHost(
            pull_request=PullRequest(
                repo=REPO,
                number=42,
                title="Add login",
                body="",
                author="octocat",
                base_sha="b",
                head_sha="head5678",
            ),
            diff=Diff(
                files=(
                    ChangedFile(
                        "app/auth/login.py",
                        "modified",
                        1,
                        0,
                        (DiffHunk("app/auth/login.py", 1, 1, 1, 1, f'+password = "{secret}"'),),
                    ),
                ),
                raw=f'+password = "{secret}"',
            ),
            files={"app/auth/login.py": f'password = "{secret}"\n'},
        )
        logger = RecordingLogger()
        model = FakeChatModel(
            responses={"route": ['{"specialists": ["correctness"], "reason": "x"}']}
        )

        await run_graph(
            build(
                model=model,
                retriever=FakeRetriever(chunks=[scored(CHUNK_A)]),
                logger=logger,
                code_host=host,
            )
        )

        for line in logger.lines:
            if line.level in {"INFO", "WARN", "ERROR"}:
                assert secret not in str(line.fields), f"{line.event} leaked diff content"
