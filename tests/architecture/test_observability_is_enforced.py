"""Observability that cannot be forgotten.

Two enforcement mechanisms, both guarding the same decay: per-node instrumentation and log
documentation are exactly the things that get skipped on the fifth node added in a hurry —
which is always the one that breaks at 1am.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from app.application.agents import approval as approval_module
from app.application.agents import nodes as nodes_module
from app.application.agents.graph import NODE_TYPES
from app.application.agents.nodes import TracedNode
from app.domain import log_events
from tests.support.ast_imports import APP_ROOT, REPO_ROOT, python_files

LOGGING_DOC = REPO_ROOT / "docs" / "Logging.md"


class TestEveryNodeIsTraced:
    def test_node_types_registry_is_not_empty(self) -> None:
        """Guards against the registry being emptied and the tests below passing vacuously."""
        assert len(NODE_TYPES) >= 4

    @pytest.mark.parametrize("node_type", NODE_TYPES, ids=lambda t: t.__name__)
    def test_registered_node_inherits_the_traced_base(self, node_type: type) -> None:
        assert issubclass(node_type, TracedNode)

    @pytest.mark.parametrize("node_type", NODE_TYPES, ids=lambda t: t.__name__)
    def test_node_has_a_name(self, node_type: type[TracedNode]) -> None:
        assert isinstance(node_type.name, str) and node_type.name

    def test_node_names_are_unique(self) -> None:
        names = [t.name for t in NODE_TYPES]
        assert len(names) == len(set(names))

    def test_every_traced_node_subclass_is_registered(self) -> None:
        """A node class that exists but is not in the registry is a node nobody traced.

        This is the failure the registry exists to catch: someone writes ``PublishNode``,
        adds it to the graph, and forgets ``NODE_TYPES``. Then it has no coverage here and
        its absence is invisible.
        """
        defined = {
            obj
            for module in (nodes_module, approval_module)
            for obj in vars(module).values()
            if inspect.isclass(obj) and issubclass(obj, TracedNode) and obj is not TracedNode
        }
        assert defined == set(NODE_TYPES), (
            "TracedNode subclasses not registered in graph.NODE_TYPES: "
            f"{sorted(t.__name__ for t in defined - set(NODE_TYPES))}"
        )

    @pytest.mark.parametrize("node_type", NODE_TYPES, ids=lambda t: t.__name__)
    def test_node_does_not_override_the_traced_call(self, node_type: type[TracedNode]) -> None:
        """``__call__`` is ``@final``. Overriding it would skip instrumentation entirely."""
        assert "__call__" not in vars(node_type)

    @pytest.mark.parametrize("node_type", NODE_TYPES, ids=lambda t: t.__name__)
    def test_node_implements_run_not_call(self, node_type: type[TracedNode]) -> None:
        assert "run" in vars(node_type)

    def test_the_base_class_emits_the_lifecycle_events(self) -> None:
        """The instrumentation is in one place; assert that place actually instruments."""
        source = inspect.getsource(TracedNode.__call__)
        assert "NODE_STARTED" in source
        assert "NODE_COMPLETED" in source
        assert "NODE_FAILED" in source


class TestLogEventsAreDocumented:
    def test_catalogue_is_not_empty(self) -> None:
        assert len(log_events.ALL_EVENTS) >= 25

    def test_all_events_matches_the_declared_constants(self) -> None:
        """``ALL_EVENTS`` is hand-maintained; drift makes the documentation test lie."""
        declared = {
            value
            for name, value in vars(log_events).items()
            if name.isupper() and name != "ALL_EVENTS" and isinstance(value, str)
        }
        assert declared == set(log_events.ALL_EVENTS)

    @pytest.mark.parametrize("event", sorted(log_events.ALL_EVENTS))
    def test_every_event_is_documented(self, event: str) -> None:
        """Undocumented telemetry is noise that looks like signal."""
        assert LOGGING_DOC.exists()
        assert f"`{event}`" in LOGGING_DOC.read_text(encoding="utf-8"), (
            f"{event} is emitted but not documented in docs/Logging.md. Every event needs "
            "the question it exists to answer written down."
        )

    def test_no_bare_event_strings_at_call_sites(self) -> None:
        """Event names come from ``log_events``, never as inline literals.

        A log line is an interface: once I grep for ``route.decided`` at 1am, renaming it is
        a breaking change. Constants make the rename visible; literals hide it.
        """
        offenders: list[str] = []
        known = set(log_events.ALL_EVENTS)

        for path in python_files(APP_ROOT):
            if path.name == "log_events.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                method = getattr(node.func, "attr", None)
                if method not in {"info", "warn", "error", "debug"}:
                    continue
                if not node.args:
                    continue
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    location = f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}"
                    hint = "unknown event" if first.value not in known else "use the constant"
                    offenders.append(f"{location} logs {first.value!r} ({hint})")

        assert not offenders, "\n".join(offenders)
