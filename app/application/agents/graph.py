"""The review graph.

```
ingest ──► route ──► specialists ──► synthesise ──► END
```

Phase 5 inserts ``interrupt()`` and ``publish`` after ``synthesise``. The graph deliberately
ends at ``synthesise`` today rather than stubbing a publish node, because a stub that posts
nothing is indistinguishable in a test from a guard that refuses to post — and that is the
one distinction this project cannot afford to blur.

Nodes are registered through :data:`NODE_TYPES`. ``test_every_node_is_traced`` reads that
tuple, so a node added to the graph without going through it is caught.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.application.agents.nodes import (
    IngestNode,
    RouteNode,
    SpecialistsNode,
    SynthesiseNode,
    TracedNode,
)
from app.application.agents.state import ReviewState

NODE_TYPES: tuple[type[TracedNode], ...] = (
    IngestNode,
    RouteNode,
    SpecialistsNode,
    SynthesiseNode,
)
"""Every node type in the graph. The registry `test_every_node_is_traced` reads."""


def build_review_graph(
    *,
    ingest: IngestNode,
    route: RouteNode,
    specialists: SpecialistsNode,
    synthesise: SynthesiseNode,
    checkpointer: Any | None = None,
) -> Any:
    """Wire the graph from already-constructed nodes.

    Nodes are injected rather than built here so that the composition root owns every
    dependency decision and this function stays a description of control flow. It also means
    a test can pass fakes without patching anything.
    """
    for node in (ingest, route, specialists, synthesise):
        if not isinstance(node, TracedNode):
            raise TypeError(
                f"{type(node).__name__} is not a TracedNode. Every graph node must inherit "
                "the traced base class, so that a node cannot be added without observability."
            )

    builder: StateGraph[ReviewState, None, ReviewState, ReviewState] = StateGraph(ReviewState)
    builder.add_node(ingest.name, ingest)
    builder.add_node(route.name, route)
    builder.add_node(specialists.name, specialists)
    builder.add_node(synthesise.name, synthesise)

    builder.add_edge(START, ingest.name)
    builder.add_edge(ingest.name, route.name)
    builder.add_edge(route.name, specialists.name)
    builder.add_edge(specialists.name, synthesise.name)
    builder.add_edge(synthesise.name, END)

    return builder.compile(checkpointer=checkpointer)
