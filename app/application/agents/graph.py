"""The review graph.

```
ingest ──► route ──► specialists ──► synthesise ──► approval ──► publish ──► END
                                                        │
                                                 interrupt(): durable,
                                                 the process may die here
```

``approval`` checkpoints state and blocks on ``interrupt()``. ``publish`` re-reads the audit
log for every finding rather than trusting the state that came back through the resume — the
approval trail is the record, and state travelled through a checkpoint to get here.

Nodes are registered through :data:`NODE_TYPES`. ``test_every_node_is_traced`` reads that
tuple, so a node added to the graph without going through it is caught.
"""

from __future__ import annotations

import itertools
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.application.agents.approval import ApprovalNode, PublishNode
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
    ApprovalNode,
    PublishNode,
)
"""Every node type in the graph. The registry `test_every_node_is_traced` reads."""


def build_review_graph(
    *,
    ingest: IngestNode,
    route: RouteNode,
    specialists: SpecialistsNode,
    synthesise: SynthesiseNode,
    approval: ApprovalNode | None = None,
    publish: PublishNode | None = None,
    checkpointer: Any | None = None,
) -> Any:
    """Wire the graph from already-constructed nodes.

    Nodes are injected rather than built here so that the composition root owns every
    dependency decision and this function stays a description of control flow. It also means
    a test can pass fakes without patching anything.
    """
    stages = [ingest, route, specialists, synthesise]
    # why: approval and publish are optional so a caller that only wants findings -- the
    #      MCP server, the eval harness -- gets a graph with no write path at all, rather
    #      than a write path it is trusted not to reach. Absence beats discipline.
    #      alt: always build them and gate at the edge (one bug from an unapproved post)
    if approval is not None:
        stages.append(approval)
    if publish is not None:
        stages.append(publish)

    for node in stages:
        if not isinstance(node, TracedNode):
            raise TypeError(
                f"{type(node).__name__} is not a TracedNode. Every graph node must inherit "
                "the traced base class, so that a node cannot be added without observability."
            )

    builder: StateGraph[ReviewState, None, ReviewState, ReviewState] = StateGraph(ReviewState)
    for node in stages:
        builder.add_node(node.name, node)

    builder.add_edge(START, stages[0].name)
    for earlier, later in itertools.pairwise(stages):
        builder.add_edge(earlier.name, later.name)
    builder.add_edge(stages[-1].name, END)

    return builder.compile(checkpointer=checkpointer)
