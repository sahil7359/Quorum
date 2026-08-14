from __future__ import annotations

import pytest

from app.infrastructure.config import Settings


def test_settings_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUORUM_LLM_PROVIDER", "groq")
    monkeypatch.setenv("QUORUM_MAX_DIFF_LINES", "42")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]  # pydantic-settings runtime kwarg

    assert settings.llm_provider == "groq"
    assert settings.max_diff_lines == 42


def test_model_routing_follows_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Specialists get the small model, synthesis gets the big one -- in both providers."""
    monkeypatch.setenv("QUORUM_LLM_PROVIDER", "groq")
    groq = Settings(_env_file=None)  # type: ignore[call-arg]
    assert groq.specialist_model == "llama-3.1-8b-instant"
    assert groq.synthesis_model == "llama-3.3-70b-versatile"

    monkeypatch.setenv("QUORUM_LLM_PROVIDER", "ollama")
    ollama = Settings(_env_file=None)  # type: ignore[call-arg]
    assert ollama.specialist_model == "llama3.1:8b"
    assert ollama.synthesis_model == "qwen3-coder:30b"


def test_github_token_is_not_in_mcp_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guardrail G12: argv is world-readable via ``ps``; the token goes by environment."""
    monkeypatch.setenv("QUORUM_GITHUB_TOKEN", "ghp_thisisafaketokenforatest0000000000")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.github_token not in " ".join(settings.github_mcp_argv)
    assert not any("ghp_" in arg for arg in settings.github_mcp_argv)


class TestConfigHash:
    """The cache key must change whenever the *output* of a review could change.

    A cache that misses a prompt change is worse than no cache: it is confidently wrong.
    """

    @pytest.mark.parametrize(
        ("env_var", "value"),
        [
            ("QUORUM_PROMPT_VERSION", "2"),
            ("QUORUM_CHUNKER_VERSION", "2"),
            ("QUORUM_RETRIEVAL_TOP_K", "7"),
            ("QUORUM_RETRIEVAL_CANDIDATES", "50"),
            ("QUORUM_RERANK_ENABLED", "false"),
            ("QUORUM_EMBEDDING_MODEL", "BAAI/bge-base-en-v1.5"),
            ("QUORUM_RERANK_MODEL", "other/model"),
            ("QUORUM_MAX_DIFF_LINES", "500"),
            ("QUORUM_LLM_PROVIDER", "groq"),
        ],
    )
    def test_output_affecting_settings_change_the_hash(
        self, monkeypatch: pytest.MonkeyPatch, env_var: str, value: str
    ) -> None:
        baseline = Settings(_env_file=None).config_hash()  # type: ignore[call-arg]
        monkeypatch.setenv(env_var, value)
        changed = Settings(_env_file=None).config_hash()  # type: ignore[call-arg]

        assert changed != baseline, f"{env_var} changes review output but not the cache key"

    @pytest.mark.parametrize(
        ("env_var", "value"),
        [
            ("QUORUM_LOG_LEVEL", "DEBUG"),
            ("QUORUM_GROQ_API_KEY", "gsk_not_a_real_key"),
            ("QUORUM_DATABASE_URL", "postgresql+psycopg://x:y@z/db"),
            ("QUORUM_OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
        ],
    )
    def test_transport_and_secret_settings_do_not_change_the_hash(
        self, monkeypatch: pytest.MonkeyPatch, env_var: str, value: str
    ) -> None:
        """These change *how* we reach a provider, not *what* the review says."""
        baseline = Settings(_env_file=None).config_hash()  # type: ignore[call-arg]
        monkeypatch.setenv(env_var, value)

        assert Settings(_env_file=None).config_hash() == baseline  # type: ignore[call-arg]

    def test_hash_is_stable_across_instances(self) -> None:
        assert Settings(_env_file=None).config_hash() == Settings(_env_file=None).config_hash()  # type: ignore[call-arg]

    def test_no_secret_material_leaks_into_the_hash_input(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("QUORUM_GROQ_API_KEY", "gsk_secret")
        monkeypatch.setenv("QUORUM_GITHUB_TOKEN", "ghp_secret")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]

        assert "secret" not in settings.config_hash()
