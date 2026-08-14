"""AST helpers for the architecture fitness tests.

These tests parse source rather than importing it. Importing a module to inspect its
dependencies is circular reasoning -- a module that violates a boundary would have to be
imported (and therefore succeed) before the violation could be detected. Parsing catches
the violation without executing anything.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = REPO_ROOT / "app"


@dataclass(frozen=True, slots=True)
class ImportSite:
    """One import statement, located precisely enough to fix without searching."""

    module: str
    """Top-level package name, e.g. ``httpx`` for ``from httpx import AsyncClient``."""

    full_target: str
    path: Path
    lineno: int

    def __str__(self) -> str:
        rel = self.path.relative_to(REPO_ROOT).as_posix()
        return f"{rel}:{self.lineno} imports {self.full_target!r}"


def python_files(package_dir: Path) -> Iterator[Path]:
    yield from sorted(package_dir.rglob("*.py"))


def imports_in_file(path: Path) -> Iterator[ImportSite]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield ImportSite(
                    module=alias.name.split(".")[0],
                    full_target=alias.name,
                    path=path,
                    lineno=node.lineno,
                )
        # why: level > 0 is a relative import. Ruff's TID rules ban those repo-wide, so
        #      treating them as unresolvable here would be dead code; they cannot occur.
        #      alt: resolve them against the package path (more code, zero coverage)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield ImportSite(
                module=node.module.split(".")[0],
                full_target=node.module,
                path=path,
                lineno=node.lineno,
            )


def imports_in_package(package_dir: Path) -> Iterator[ImportSite]:
    for path in python_files(package_dir):
        yield from imports_in_file(path)


def is_stdlib(module: str) -> bool:
    """True for standard-library top-level modules.

    Uses ``sys.stdlib_module_names`` (3.10+) rather than a hand-maintained list, so the set
    cannot drift from the interpreter the tests actually run on.
    """
    return module in sys.stdlib_module_names or module == "__future__"
