"""Run the import-linter contracts inside the test suite, not only in CI.

A boundary check that lives only in a CI job is a check I do not see until I push. Running
it in pytest means a violation shows up in the same red bar as everything else.
"""

from __future__ import annotations

import shutil
import subprocess

from tests.support.ast_imports import REPO_ROOT


def test_import_linter_contracts_hold() -> None:
    executable = shutil.which("lint-imports")
    assert executable is not None, (
        "lint-imports not found on PATH. Run the suite with `uv run pytest` so the "
        "project virtualenv is active. This is deliberately an assertion and not a skip: "
        "a silently skipped architecture check is the same as no architecture check."
    )

    # Fixed executable resolved from PATH, no user-controlled arguments.
    result = subprocess.run(
        [executable, "--config", str(REPO_ROOT / "pyproject.toml")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, (
        "import-linter contracts failed:\n" + result.stdout + "\n" + result.stderr
    )
