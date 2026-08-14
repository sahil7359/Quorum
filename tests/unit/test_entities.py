from __future__ import annotations

import pytest

from app.domain.entities import (
    Approval,
    CacheKey,
    ChangedFile,
    Chunk,
    Citation,
    Diff,
    Finding,
    PullRequest,
    RepoRef,
    RoutingDecision,
    rank_findings,
)
from app.domain.values import (
    ApprovalAction,
    ChunkId,
    ChunkLocator,
    FindingId,
    RunId,
    Severity,
    SpecialistKind,
)


def a_locator(offset: int = 0) -> ChunkLocator:
    return ChunkLocator(
        repo="acme/widget",
        commit_sha="cafe",
        file_path="CONTRIBUTING.md",
        section_path="Testing",
        start_offset=offset,
        end_offset=offset + 100,
    )


def a_finding(
    *, severity: Severity = Severity.MEDIUM, confidence: float = 0.5, title: str = "t"
) -> Finding:
    loc = a_locator()
    return Finding(
        finding_id=FindingId.new(),
        specialist=SpecialistKind.CORRECTNESS,
        severity=severity,
        confidence=confidence,
        title=title,
        body="b",
        citation=Citation(chunk_id=ChunkId.derive(loc), locator=loc),
    )


class TestRepoRef:
    def test_parse(self) -> None:
        assert str(RepoRef.parse("psf/requests")) == "psf/requests"

    @pytest.mark.parametrize("bad", ["noslash", "a/b/c", "../etc/passwd", "/leading"])
    def test_rejects_malformed(self, bad: str) -> None:
        """Repo strings reach a URL eventually. Validate at construction, not at the edge."""
        with pytest.raises(ValueError):
            RepoRef.parse(bad)


class TestChunkIntegrity:
    def test_chunk_rejects_an_id_that_does_not_derive_from_its_locator(self) -> None:
        """Guards against the chunker and the id scheme silently drifting apart."""
        with pytest.raises(ValueError, match="not derived from its locator"):
            Chunk(
                chunk_id=ChunkId.derive(a_locator(999)),
                locator=a_locator(0),
                content="x",
                start_line=1,
                end_line=2,
                ordinal=0,
                token_count=1,
            )

    def test_create_builds_a_consistent_pair(self) -> None:
        chunk = Chunk.create(a_locator(), "x", start_line=1, end_line=2, ordinal=0, token_count=1)
        assert chunk.chunk_id.matches(chunk.locator)

    def test_citation_rejects_a_mismatched_pair(self) -> None:
        with pytest.raises(ValueError, match="does not derive"):
            Citation(chunk_id=ChunkId.derive(a_locator(999)), locator=a_locator(0))


class TestDiffHeuristics:
    @pytest.mark.parametrize(
        "path",
        [
            "tests/test_auth.py",
            "test_auth.py",
            "src/auth_test.py",
            "app/components/Button.test.tsx",
            "spec/models_spec.py",
        ],
    )
    def test_test_files_are_recognised(self, path: str) -> None:
        assert ChangedFile(path, "modified", 1, 0).is_test_file

    @pytest.mark.parametrize("path", ["app/auth/login.py", "src/latest.py", "docs/contest.md"])
    def test_source_files_are_not_mistaken_for_tests(self, path: str) -> None:
        """``latest.py`` and ``contest.md`` contain "test" as a substring. They are not tests."""
        assert not ChangedFile(path, "modified", 1, 0).is_test_file

    def test_source_change_without_test_change_is_visible(self) -> None:
        diff = Diff(
            files=(ChangedFile("app/auth/login.py", "modified", 40, 2),),
            raw="--- a\n+++ b\n",
        )
        assert diff.has_source_changes
        assert not diff.has_test_changes

    def test_truncation_is_carried_on_the_entity(self) -> None:
        """A silently truncated review is a lie about coverage, so it travels with the data."""
        diff = Diff(files=(), raw="x", truncated=True, truncated_at_line=1500)
        assert diff.truncated
        assert diff.truncated_at_line == 1500


class TestRoutingDecision:
    def test_correctness_is_unconditional(self) -> None:
        with pytest.raises(ValueError, match="correctness is unconditional"):
            RoutingDecision(specialists=(SpecialistKind.SECURITY,), reason="only security")

    def test_reason_is_required(self) -> None:
        """A routing decision without a reason is not debuggable, so it is not constructible."""
        with pytest.raises(ValueError, match="not debuggable"):
            RoutingDecision(specialists=(SpecialistKind.CORRECTNESS,), reason="   ")

    def test_attribution_is_kept_separate(self) -> None:
        """Heuristic floor vs LLM addition, so a bad call is attributable to one of them."""
        decision = RoutingDecision(
            specialists=(SpecialistKind.CORRECTNESS, SpecialistKind.SECURITY),
            reason="diff touches app/auth/; 2 security path globs matched",
            heuristic_floor=(SpecialistKind.CORRECTNESS,),
            llm_added=(SpecialistKind.SECURITY,),
        )
        assert decision.heuristic_floor != decision.llm_added


class TestFindingRankingAndHashing:
    def test_ranking_is_severity_then_confidence(self) -> None:
        findings = [
            a_finding(severity=Severity.LOW, confidence=0.99, title="low-certain"),
            a_finding(severity=Severity.HIGH, confidence=0.20, title="high-unsure"),
            a_finding(severity=Severity.HIGH, confidence=0.90, title="high-certain"),
        ]

        assert [f.title for f in rank_findings(findings)] == [
            "high-certain",
            "high-unsure",
            "low-certain",
        ]

    def test_payload_hash_changes_when_text_changes(self) -> None:
        """Guardrail G5: an approval is bound to exact text."""
        original = a_finding(title="Missing null check")
        edited = Finding(
            finding_id=original.finding_id,
            specialist=original.specialist,
            severity=original.severity,
            confidence=original.confidence,
            title="Missing null check (edited)",
            body=original.body,
            citation=original.citation,
        )

        assert original.payload_hash != edited.payload_hash

    def test_approval_does_not_authorise_edited_text(self) -> None:
        original = a_finding(title="Missing null check")
        approval = Approval(
            run_id=RunId.new(),
            finding_id=original.finding_id,
            action=ApprovalAction.APPROVED,
            actor="sahil",
            payload_hash=original.payload_hash,
        )
        assert approval.authorises(original)

        edited = Finding(
            finding_id=original.finding_id,
            specialist=original.specialist,
            severity=original.severity,
            confidence=original.confidence,
            title="Something entirely different",
            body=original.body,
            citation=original.citation,
        )
        assert not approval.authorises(edited)

    def test_rejection_never_authorises(self) -> None:
        finding = a_finding()
        approval = Approval(
            run_id=RunId.new(),
            finding_id=finding.finding_id,
            action=ApprovalAction.REJECTED,
            actor="sahil",
            payload_hash=finding.payload_hash,
        )
        assert not approval.authorises(finding)


class TestCacheKey:
    def test_head_sha_changes_the_key(self) -> None:
        base = CacheKey(RepoRef.parse("a/b"), 1, "sha1", "cfg")
        assert base.value() != CacheKey(RepoRef.parse("a/b"), 1, "sha2", "cfg").value()

    def test_config_hash_changes_the_key(self) -> None:
        """A prompt change must not serve a review the current code would not produce."""
        base = CacheKey(RepoRef.parse("a/b"), 1, "sha1", "cfg1")
        assert base.value() != CacheKey(RepoRef.parse("a/b"), 1, "sha1", "cfg2").value()

    def test_key_is_deterministic(self) -> None:
        args = (RepoRef.parse("a/b"), 1, "sha1", "cfg")
        assert CacheKey(*args).value() == CacheKey(*args).value()


def test_pull_request_number_must_be_positive() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        PullRequest(
            repo=RepoRef.parse("a/b"),
            number=0,
            title="t",
            body="b",
            author="a",
            base_sha="x",
            head_sha="y",
        )
