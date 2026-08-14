"""Invariant: ``app.application`` performs no I/O of its own.

LangGraph is permitted in this layer (ADR-0002) because the graph *is* the application
logic. That exception is narrow: control flow yes, sockets and sessions no. Everything the
agents touch arrives through a domain port.

Banning ``pathlib`` and ``os`` here has a second effect I want: prompts cannot be loaded
from disk, so they must be constants in code. That is guardrail G2 -- a system prompt with
no interpolation point -- enforced structurally rather than by review.
"""

from __future__ import annotations

from tests.support.ast_imports import APP_ROOT, imports_in_package

APPLICATION = APP_ROOT / "application"

BANNED = frozenset(
    {
        # network
        "httpx",
        "requests",
        "urllib",
        "urllib3",
        "socket",
        "http",
        # database
        "sqlalchemy",
        "psycopg",
        "psycopg2",
        "sqlite3",
        # integrations that belong behind a port
        "mcp",
        "fastembed",
        "fastapi",
        "openai",
        "groq",
        # process and filesystem
        "subprocess",
        "os",
        "pathlib",
        "shutil",
        "tempfile",
    }
)


def test_application_does_not_perform_io() -> None:
    violations = [site for site in imports_in_package(APPLICATION) if site.module in BANNED]
    assert not violations, (
        "app/application must reach the outside world only through a domain port.\n"
        + "\n".join(f"  - {v}" for v in violations)
    )


def test_application_does_not_import_infrastructure() -> None:
    violations = [
        site
        for site in imports_in_package(APPLICATION)
        if site.full_target.startswith("app.infrastructure")
    ]
    assert not violations, (
        "application and infrastructure are siblings; neither may import the other.\n"
        + "\n".join(f"  - {v}" for v in violations)
    )


def test_infrastructure_does_not_import_application() -> None:
    violations = [
        site
        for site in imports_in_package(APP_ROOT / "infrastructure")
        if site.full_target.startswith("app.application")
    ]
    assert not violations, (
        "application and infrastructure are siblings; neither may import the other.\n"
        + "\n".join(f"  - {v}" for v in violations)
    )


def test_banned_set_is_non_empty_and_covers_the_obvious_offenders() -> None:
    """Guard against the banned list being emptied to make the suite green."""
    assert {"httpx", "sqlalchemy", "mcp", "fastapi", "subprocess"} <= BANNED
