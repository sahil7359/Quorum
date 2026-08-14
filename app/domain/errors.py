"""Domain errors.

Every one of these is a condition the design anticipates, not a bug. Anything unanticipated
should surface as its own exception type rather than being flattened into ``QuorumError``,
because a caught-and-logged unknown failure is how a silent degradation starts.
"""

from __future__ import annotations


class QuorumError(Exception):
    """Base for every anticipated failure."""


class DiffTooLargeError(QuorumError):
    """The diff exceeds the configured cap and truncation was not permitted."""

    def __init__(self, actual_lines: int, limit: int) -> None:
        super().__init__(f"diff has {actual_lines} lines, limit is {limit}")
        self.actual_lines = actual_lines
        self.limit = limit


class BudgetExhaustedError(QuorumError):
    """The daily token budget is spent.

    Raised rather than silently degrading: falling back to a cached review is a decision the
    caller makes explicitly and reports honestly to the user.
    """

    def __init__(self, consumed: int, limit: int) -> None:
        super().__init__(f"daily token budget exhausted: {consumed}/{limit}")
        self.consumed = consumed
        self.limit = limit


class ToolNotAllowedError(QuorumError):
    """An MCP tool outside the allowlist was requested. Guardrail G3."""

    def __init__(self, tool_name: str) -> None:
        super().__init__(f"tool {tool_name!r} is not on the allowlist")
        self.tool_name = tool_name


class ApprovalRequiredError(QuorumError):
    """A write was attempted without a matching approved audit row. Guardrail G4.

    This is the last guard before GitHub. It exists even though the graph should never route
    here without approval, because graph topology is a convention and a guard is not.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(f"refusing to write: {detail}")


class CodeHostError(QuorumError):
    """The code host (GitHub, via MCP) could not serve the request."""


class SpecialistFailedError(QuorumError):
    """One specialist produced unusable output.

    Handled, not fatal: the specialist is dropped and the others still produce a review. One
    bad actor does not fail the run.
    """

    def __init__(self, specialist: str, detail: str) -> None:
        super().__init__(f"specialist {specialist} failed: {detail}")
        self.specialist = specialist


class AllSpecialistsFailedError(QuorumError):
    """Every specialist failed. This *is* fatal -- an empty review is not a clean review."""
