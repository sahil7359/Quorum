"""The log event catalogue.

Every structured log event Quorum emits has a name declared here, and every name is
documented in ``docs/Logging.md`` with the question it exists to answer.

Two reasons this is a module of constants rather than string literals at call sites:

1. **A log line is an interface.** Once I grep for ``route.decided`` at 1am, renaming it is
   a breaking change. Constants make the rename visible.
2. **It makes the documentation enforceable.** ``test_every_log_event_is_documented``
   asserts that every constant here appears in ``docs/Logging.md``, so an event cannot be
   added without saying why it exists. Undocumented telemetry is noise that looks like signal.

Naming is ``noun.verb_past`` (``retrieval.completed``) or ``noun.state`` (``budget.exhausted``)
so that events sort into coherent groups when read as a stream.
"""

from __future__ import annotations

from typing import Final

# --- run lifecycle ---------------------------------------------------------
RUN_STARTED: Final = "run.started"
RUN_COMPLETED: Final = "run.completed"
RUN_FAILED: Final = "run.failed"

# --- graph nodes -----------------------------------------------------------
NODE_STARTED: Final = "node.started"
NODE_COMPLETED: Final = "node.completed"
NODE_FAILED: Final = "node.failed"

# --- tracing spans ----------------------------------------------------------
# why: StructlogTracer's spans are structured log lines, not a separate tracing protocol
#      (see infrastructure/observability/tracing.py's module docstring) -- so they go through
#      the same event catalogue and documentation requirement as everything else, rather than
#      being a second, undocumented category of "thing this project logs".
SPAN_STARTED: Final = "span.started"
SPAN_COMPLETED: Final = "span.completed"
SPAN_FAILED: Final = "span.failed"

# --- the routing decision --------------------------------------------------
ROUTE_DECIDED: Final = "route.decided"
ROUTE_LLM_IGNORED: Final = "route.llm_removal_ignored"
ROUTE_LLM_UNPARSEABLE: Final = "route.llm_unparseable"

# --- retrieval -------------------------------------------------------------
RETRIEVAL_COMPLETED: Final = "retrieval.completed"

# --- specialists and findings ----------------------------------------------
SPECIALIST_STARTED: Final = "specialist.started"
SPECIALIST_COMPLETED: Final = "specialist.completed"
SPECIALIST_FAILED: Final = "specialist.failed"
FINDING_RAISED: Final = "finding.raised"
FINDING_DROPPED: Final = "finding.dropped"

# --- LLM calls -------------------------------------------------------------
LLM_CALLED: Final = "llm.called"
LLM_FAILED: Final = "llm.failed"

# --- context scoping -------------------------------------------------------
CONTEXT_SCOPED: Final = "context.scoped"

# --- MCP -------------------------------------------------------------------
MCP_CONNECTED: Final = "mcp.connected"
MCP_TOOLS_UNVETTED: Final = "mcp.tools.unvetted_available"
MCP_TOOL_CALL: Final = "mcp.tool.call"
MCP_TOOL_REFUSED: Final = "mcp.tool.refused"

# --- diff ------------------------------------------------------------------
DIFF_TRUNCATED: Final = "diff.truncated"

# --- cost controls ---------------------------------------------------------
CACHE_HIT: Final = "cache.hit"
CACHE_MISS: Final = "cache.miss"
BUDGET_RESERVED: Final = "budget.reserved"
BUDGET_EXHAUSTED: Final = "budget.exhausted"
RATE_LIMITED: Final = "rate_limit.exceeded"

# --- approval and publish --------------------------------------------------
APPROVAL_PROPOSED: Final = "approval.proposed"
APPROVAL_DECIDED: Final = "approval.decided"
PUBLISH_POSTED: Final = "publish.posted"
PUBLISH_REFUSED: Final = "publish.refused"


ALL_EVENTS: Final[frozenset[str]] = frozenset(
    {
        RUN_STARTED,
        RUN_COMPLETED,
        RUN_FAILED,
        NODE_STARTED,
        NODE_COMPLETED,
        NODE_FAILED,
        SPAN_STARTED,
        SPAN_COMPLETED,
        SPAN_FAILED,
        ROUTE_DECIDED,
        ROUTE_LLM_IGNORED,
        ROUTE_LLM_UNPARSEABLE,
        RETRIEVAL_COMPLETED,
        SPECIALIST_STARTED,
        SPECIALIST_COMPLETED,
        SPECIALIST_FAILED,
        FINDING_RAISED,
        FINDING_DROPPED,
        LLM_CALLED,
        LLM_FAILED,
        CONTEXT_SCOPED,
        MCP_CONNECTED,
        MCP_TOOLS_UNVETTED,
        MCP_TOOL_CALL,
        MCP_TOOL_REFUSED,
        DIFF_TRUNCATED,
        CACHE_HIT,
        CACHE_MISS,
        BUDGET_RESERVED,
        BUDGET_EXHAUSTED,
        RATE_LIMITED,
        APPROVAL_PROPOSED,
        APPROVAL_DECIDED,
        PUBLISH_POSTED,
        PUBLISH_REFUSED,
    }
)
