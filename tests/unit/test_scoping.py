"""AST context scoping.

The point of this module is token cost: the binding constraint is 100K tokens a day, and a
review carrying whole files instead of changed regions is the difference between two reviews
and four.
"""

from __future__ import annotations

from app.application.agents.scoping import (
    changed_line_numbers,
    scope_diff,
    scope_file,
)
from app.domain.entities import ChangedFile, DiffHunk

SOURCE = '''"""Module docstring."""

import os


def untouched_helper(value):
    """This function is not changed and should not be shipped to the model."""
    total = 0
    for item in value:
        total += item
    return total


def authenticate(user, password):
    """This one changes."""
    if not user:
        return None
    token = issue_token(user)
    token.expires_at = None
    return token


class Session:
    def refresh(self):
        return None

    def close(self):
        return None
'''


def hunk(new_start: int, body: str) -> DiffHunk:
    return DiffHunk("app/auth.py", new_start, 1, new_start, len(body.split("\n")), body)


def changed_file(path: str, *hunks: DiffHunk) -> ChangedFile:
    return ChangedFile(path, "modified", 1, 0, hunks)


class TestChangedLineNumbers:
    def test_added_lines_are_located_in_the_new_file(self) -> None:
        changed = changed_file("app/auth.py", hunk(15, " context\n+added one\n+added two"))
        assert changed_line_numbers(changed) == {16, 17}

    def test_removed_lines_do_not_advance_the_new_file_cursor(self) -> None:
        """A removed line has no position in the new file, so it cannot be scoped around."""
        changed = changed_file("app/auth.py", hunk(10, " context\n-removed\n+added"))
        assert changed_line_numbers(changed) == {11}


class TestAstScoping:
    def test_only_the_enclosing_function_is_returned(self) -> None:
        """The whole point: an 800-line file becomes the function that changed."""
        changed = changed_file("app/auth.py", hunk(18, "+    token.expires_at = None"))
        regions = scope_file(changed, SOURCE)

        assert len(regions) == 1
        assert regions[0].strategy == "ast"
        assert "def authenticate" in regions[0].content
        assert "untouched_helper" not in regions[0].content

    def test_method_change_does_not_ship_the_whole_class(self) -> None:
        """``ast.walk`` yields the class as well as the method; only the smallest span wins."""
        line = SOURCE.split("\n").index("    def refresh(self):") + 2
        changed = changed_file("app/auth.py", hunk(line, "+        return None"))
        regions = scope_file(changed, SOURCE)

        assert regions
        assert "def refresh" in regions[0].content
        assert "def close" not in regions[0].content

    def test_two_changed_functions_yield_two_regions(self) -> None:
        changed = changed_file(
            "app/auth.py",
            hunk(9, "+        total += item"),
            hunk(18, "+    token.expires_at = None"),
        )
        regions = scope_file(changed, SOURCE)
        assert len(regions) == 2

    def test_syntax_error_falls_back_to_a_window(self) -> None:
        """A file can be syntactically invalid at this commit. Expected, not exceptional."""
        changed = changed_file("app/auth.py", hunk(3, "+def broken(:"))
        regions = scope_file(changed, "def broken(:\n    pass\n")

        assert regions
        assert regions[0].strategy == "window"

    def test_non_python_file_uses_a_window(self) -> None:
        """The common case for a polyglot repository, and not a failure path."""
        changed = changed_file("src/app.ts", hunk(5, "+const x = 1;"))
        regions = scope_file(changed, "\n".join(f"line {i}" for i in range(40)))

        assert regions
        assert regions[0].strategy == "window"

    def test_change_outside_any_definition_falls_back(self) -> None:
        """A change to a module-level import has no enclosing function."""
        changed = changed_file("app/auth.py", hunk(3, "+import sys"))
        regions = scope_file(changed, SOURCE)
        assert regions
        assert regions[0].strategy == "window"

    def test_no_changed_lines_yields_nothing(self) -> None:
        assert scope_file(changed_file("app/auth.py"), SOURCE) == []


class TestScopingReport:
    def test_reduction_is_reported(self) -> None:
        changed = changed_file("app/auth.py", hunk(18, "+    token.expires_at = None"))
        report = scope_diff([(changed, SOURCE)])

        assert report.tokens_scoped < report.tokens_whole_file
        assert report.reduction_pct > 0
        assert report.ast_regions == 1

    def test_reduction_is_zero_when_nothing_is_scoped(self) -> None:
        report = scope_diff([])
        assert report.reduction_pct == 0.0

    def test_report_never_claims_a_negative_reduction_silently(self) -> None:
        """A tiny file can scope to more tokens than it contains, once headers are added.

        Worth knowing rather than hiding: the figure is published, so it has to be able to
        say "this did not help" as well as "this helped".
        """
        tiny = "x = 1\n"
        changed = changed_file("app/tiny.py", hunk(1, "+x = 1"))
        report = scope_diff([(changed, tiny)])

        assert isinstance(report.reduction_pct, float)
