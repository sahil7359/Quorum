"""Invariant: no module under ``app/domain`` imports anything outside the standard library.

This is the load-bearing architectural rule (ADR-0001). It is enforced here *and* by an
import-linter contract, because a config file can be relaxed in the same commit that breaks
the rule, whereas weakening a test is visible as weakening a test.

Proven to fail before being committed by temporarily adding ``import httpx`` to a domain
module -- recorded in HANDOFF.md, Phase 0.
"""

from __future__ import annotations

from tests.support.ast_imports import APP_ROOT, ImportSite, imports_in_package, is_stdlib

DOMAIN = APP_ROOT / "domain"


def _violations() -> list[ImportSite]:
    violations: list[ImportSite] = []
    for site in imports_in_package(DOMAIN):
        if is_stdlib(site.module):
            continue
        # The domain may refer to itself, and only to itself, within the app package.
        if site.module == "app" and site.full_target.startswith("app.domain"):
            continue
        violations.append(site)
    return violations


def test_domain_imports_only_stdlib_and_itself() -> None:
    violations = _violations()
    assert not violations, (
        "app/domain must import only the standard library and app.domain.\n"
        + "\n".join(f"  - {v}" for v in violations)
        + "\n\nIf you need this dependency, you need a Protocol port in app/domain/ports "
        "and an adapter in app/infrastructure. See docs/adr/0001-clean-architecture.md."
    )


def test_the_purity_check_can_actually_fail(tmp_path: object) -> None:
    """A test that cannot fail is worse than no test -- so prove the detector detects.

    Rather than trusting that ``_violations`` works because it currently returns nothing,
    run the same import extraction over a file that *does* violate the rule and assert it
    is caught. This keeps the green result above meaningful.
    """
    from pathlib import Path

    from tests.support.ast_imports import imports_in_file

    assert isinstance(tmp_path, Path)
    offender = tmp_path / "offending_domain_module.py"
    offender.write_text(
        "import httpx\nfrom sqlalchemy import select\nimport json\n",
        encoding="utf-8",
    )

    non_stdlib = [s.module for s in imports_in_file(offender) if not is_stdlib(s.module)]

    assert sorted(non_stdlib) == ["httpx", "sqlalchemy"]
