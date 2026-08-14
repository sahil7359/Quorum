"""Secret redaction. Guardrail G10 / T4.

The interesting risk here isn't "does it catch secrets" -- a naive high-entropy regex catches
those trivially. It's "does it *also* catch things that are not secrets": a naive first
version of this module redacted every ``run_id`` and every commit SHA in every log line,
because a UUID and a 40-character hex digest both satisfy "32+ alphanumeric characters" just
as well as a real token does. That would have silently broken "reconstructable end to end from
logs alone" -- the exact property this observability phase exists to guarantee -- while
looking, at a glance, like a working redactor. Caught empirically before it shipped; the tests
below are what would have caught it in CI.
"""

from __future__ import annotations

from app.infrastructure.observability.redaction import MASK, redact_fields, redact_text


class TestKnownSecretShapesAreRedacted:
    def test_github_classic_pat(self) -> None:
        text = redact_text("token=ghp_1234567890abcdefghijklmnopqrstuvwxyz")
        assert "ghp_" not in text
        assert MASK in text

    def test_github_fine_grained_pat(self) -> None:
        text = redact_text("token=github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz1234567890")
        assert "github_pat_" not in text
        assert MASK in text

    def test_groq_key(self) -> None:
        text = redact_text("QUORUM_GROQ_API_KEY=gsk_ABCdef1234567890ABCdef1234567890")
        assert "gsk_" not in text
        assert MASK in text

    def test_bearer_header(self) -> None:
        text = redact_text("Authorization: Bearer sk-proj-abc123XYZ789defGHI456")
        assert "sk-proj" not in text
        assert MASK in text

    def test_an_unknown_shaped_high_entropy_secret_is_still_caught(self) -> None:
        """The generic fallback exists so a credential format this project has never seen
        does not require a matching regex added here to be covered."""
        text = redact_text("leaked_value: QUORUM_TEST_MARKER_sk_live_51H8vGkJz3xQpN9wR")
        assert "QUORUM_TEST_MARKER" not in text
        assert MASK in text


class TestLegitimateIdentifiersSurvive:
    """The regression class: real identifiers this project logs constantly, that a naive
    high-entropy pattern would also match and destroy."""

    def test_a_run_id_uuid_is_not_redacted(self) -> None:
        run_id = "c9e49a28-517f-44b3-8b07-0de27fac423d"
        assert redact_text(f"run_id={run_id}") == f"run_id={run_id}"

    def test_a_commit_sha_is_not_redacted(self) -> None:
        sha = "a1b2c3d4e5f60718293a4b5c6d7e8f901234567"
        assert redact_text(f"head_sha={sha}") == f"head_sha={sha}"

    def test_a_chunk_id_is_not_redacted(self) -> None:
        chunk_id = "e082685bc4df5a82"
        assert redact_text(f"chunk_id={chunk_id}") == f"chunk_id={chunk_id}"

    def test_a_finding_id_uuid_is_not_redacted(self) -> None:
        finding_id = "a957d603-63eb-4386-9bee-301f9d17f7ad"
        assert redact_text(finding_id) == finding_id


class TestRedactFields:
    def test_redacts_nested_structures(self) -> None:
        fields = {
            "reason": "auth path, token=ghp_1234567890abcdefghijklmnopqrstuvwxyz",
            "signals": {"detail": "leaked gsk_ABCdef1234567890ABCdef1234567890"},
            "candidates": ["clean value", "another ghp_1234567890abcdefghijklmnopqr leak"],
        }
        redacted = redact_fields(fields)
        assert "ghp_" not in redacted["reason"]
        assert "gsk_" not in redacted["signals"]["detail"]
        assert "ghp_" not in redacted["candidates"][1]
        assert redacted["candidates"][0] == "clean value"

    def test_field_keys_are_never_redacted_only_values(self) -> None:
        redacted = redact_fields({"api_key": "sk_abcdefghijklmnopqrstuvwxyz0123456789"})
        assert "api_key" in redacted
        assert redacted["api_key"] == MASK

    def test_non_string_values_pass_through_unchanged(self) -> None:
        redacted = redact_fields({"count": 5, "ok": True, "ratio": 0.5, "nothing": None})
        assert redacted == {"count": 5, "ok": True, "ratio": 0.5, "nothing": None}
