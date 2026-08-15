"""Typed configuration.

Lives in ``infrastructure`` because it depends on pydantic-settings and reads the
environment -- both of which ``domain`` is forbidden from doing. Application code never
imports this module; the composition root reads it and passes plain values inward.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LlmProvider = Literal["groq", "ollama"]


class Settings(BaseSettings):
    """Runtime configuration, read from ``QUORUM_*`` environment variables or ``.env``."""

    model_config = SettingsConfigDict(
        env_prefix="QUORUM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # --- LLM provider -------------------------------------------------------
    # why: provider selection is a config value, never a code path, so that evaluation can
    #      run locally against Ollama while production runs on Groq without a diff.
    #      alt: separate entrypoints per provider (simpler wiring, guarantees drift)
    llm_provider: LlmProvider = "ollama"

    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_specialist_model: str = "llama-3.1-8b-instant"
    groq_synthesis_model: str = "llama-3.3-70b-versatile"

    ollama_base_url: str = "http://localhost:11434"
    ollama_specialist_model: str = "llama3.1:8b"
    ollama_synthesis_model: str = "qwen3-coder:30b"

    # --- Cost controls ------------------------------------------------------
    daily_token_budget: int = Field(default=100_000, ge=0)
    max_diff_lines: int = Field(default=1_500, ge=1)
    live_reviews_per_day: int = Field(default=4, ge=0)

    # why: sequential specialist dispatch is a *Groq free tier* fact (12K tokens/minute),
    #      not an architectural belief. Local Ollama has no such ceiling, so this is a knob
    #      rather than a hardcoded loop -- otherwise eval runs would be needlessly slow.
    #      alt: hardcode sequential dispatch (one less setting, punishes local eval)
    specialist_concurrency: int = Field(default=1, ge=1, le=3)

    # --- GitHub -------------------------------------------------------------
    github_token: str = ""
    github_mcp_command: str = "docker"
    github_mcp_args: str = (
        "run,-i,--rm,-e,GITHUB_PERSONAL_ACCESS_TOKEN,ghcr.io/github/github-mcp-server"
    )

    # --- Persistence --------------------------------------------------------
    # why: plain "postgresql://", not "postgresql+psycopg://" -- the +driver suffix is
    #      SQLAlchemy convention. There is no SQLAlchemy anywhere in this codebase, only
    #      psycopg directly, and psycopg.connect() cannot parse the +psycopg scheme at all
    #      ("missing '=' after ..."). Found wiring the first real caller of this value
    #      (scripts/composition.py) against a real connection, not by inspection -- nothing
    #      before that had ever passed database_url itself to psycopg; every Postgres test
    #      uses its own hardcoded plain DSN via QUORUM_TEST_DATABASE_URL instead.
    database_url: str = "postgresql://quorum:quorum@localhost:5433/quorum"

    # --- Retrieval ----------------------------------------------------------
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    rerank_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    retrieval_top_k: int = Field(default=5, ge=1)
    retrieval_candidates: int = Field(default=30, ge=1)
    # why: measured, not assumed. The Phase 3 retrieval eval scored reranking at
    #      NDCG@5 -0.0925 and Recall@5 -0.0958 against plain hybrid, for 91x the
    #      latency (780ms vs 8.6ms/query). It is off by default and kept behind a flag
    #      so the comparison stays reproducible. See docs/adr/0004-rerank-disabled.md.
    #      alt: default on because cross-encoders usually help (true in general, false here)
    rerank_enabled: bool = False
    chunker_version: str = "1"

    # --- Prompts ------------------------------------------------------------
    prompt_version: str = "1"

    # --- Observability ------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARN", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"

    @property
    def github_mcp_argv(self) -> list[str]:
        """MCP server launch arguments, split from the comma-separated env value.

        The GitHub token is deliberately absent -- it is passed by environment, never in
        argv, because argv is world-readable via ``ps``. See docs/Security.md section 2.
        """
        return [arg for arg in self.github_mcp_args.split(",") if arg]

    def config_hash(self) -> str:
        """Hash of everything that can change the *output* of a review.

        This is part of the review cache key. A cache that misses a prompt change is worse
        than no cache, because it is confidently wrong -- it serves a review the current
        code would not produce.

        Deliberately excludes secrets, URLs and log settings: those change *how* we reach a
        provider, not *what* the review says.
        """
        material = {
            "prompt_version": self.prompt_version,
            "chunker_version": self.chunker_version,
            "llm_provider": self.llm_provider,
            "specialist_model": self.specialist_model,
            "synthesis_model": self.synthesis_model,
            "retrieval_top_k": self.retrieval_top_k,
            "retrieval_candidates": self.retrieval_candidates,
            "rerank_enabled": self.rerank_enabled,
            "embedding_model": self.embedding_model,
            "rerank_model": self.rerank_model,
            "max_diff_lines": self.max_diff_lines,
        }
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]

    @property
    def specialist_model(self) -> str:
        return (
            self.groq_specialist_model
            if self.llm_provider == "groq"
            else self.ollama_specialist_model
        )

    @property
    def synthesis_model(self) -> str:
        return (
            self.groq_synthesis_model
            if self.llm_provider == "groq"
            else self.ollama_synthesis_model
        )
