"""Ports must stay ports.

The failure this guards against is subtle and common: someone adds a convenience
implementation to a port ("just a default, it's only two lines"), and suddenly
``infrastructure`` inherits behaviour from ``domain`` instead of satisfying a structural
contract. From there, the port stops being a boundary and becomes a base class.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from typing import get_type_hints

import pytest

from app.domain import ports

PORT_NAMES = sorted(name for name in dir(ports) if name.endswith("Port"))


def test_every_port_is_discovered() -> None:
    """Guard against the collection silently emptying and the tests below passing vacuously."""
    assert len(PORT_NAMES) >= 10, PORT_NAMES


@pytest.mark.parametrize("name", PORT_NAMES)
def test_port_is_a_protocol(name: str) -> None:
    port = getattr(ports, name)
    assert inspect.isclass(port)
    # ``_is_protocol`` is the marker typing sets on Protocol subclasses. ``issubclass(x,
    # Protocol)`` is not a type-checkable expression, and a plain ABC would pass it anyway.
    assert getattr(port, "_is_protocol", False), f"{name} is not a Protocol"


@pytest.mark.parametrize("name", PORT_NAMES)
def test_port_methods_have_no_implementation(name: str) -> None:
    """Every method body must be a docstring and/or ``...`` — a port has shape, not behaviour.

    Checked by parsing the AST rather than by scanning source lines. My first attempt read
    lines and treated anything that was not ``...`` as an implementation, which flagged every
    multi-line signature in the file. Source-line heuristics are exactly the wrong tool for
    a question about syntax.
    """
    port = getattr(ports, name)
    tree = ast.parse(textwrap.dedent(inspect.getsource(port)))
    class_def = tree.body[0]
    assert isinstance(class_def, ast.ClassDef)

    for node in class_def.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        statements = [
            stmt
            for stmt in node.body
            if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
        ]
        assert not statements, (
            f"{name}.{node.name} has an implementation. A port describes a contract; "
            "behaviour belongs in an adapter under app/infrastructure."
        )


@pytest.mark.parametrize("name", PORT_NAMES)
def test_port_signatures_are_fully_annotated(name: str) -> None:
    """An un-annotated port is a port mypy cannot check adapters against."""
    port = getattr(ports, name)
    for member_name, member in vars(port).items():
        if member_name.startswith("_") or not callable(member):
            continue
        hints = get_type_hints(member)
        signature = inspect.signature(member)
        for param in signature.parameters.values():
            if param.name == "self" or param.kind is param.VAR_KEYWORD:
                continue
            assert param.name in hints, f"{name}.{member_name}({param.name}) is un-annotated"
        assert "return" in hints, f"{name}.{member_name} has no return annotation"
