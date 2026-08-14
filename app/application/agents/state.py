"""The review graph's state.

A ``TypedDict`` because LangGraph merges node return values into the state dict, and a frozen
dataclass would fight that. It is the one place in the application layer that is mutable by
design, and it is worth being explicit about why: every node returns a *partial* update, and
LangGraph applies it. Nodes never mutate the state they were handed.
"""

from __future__ import annotations

from typing import TypedDict

from app.domain.entities import (
    CandidateFinding,
    Chunk,
    Diff,
    Finding,
    PullRequest,
    RepoRef,
    RoutingDecision,
)
from app.domain.values import ChunkId, RunId, SpecialistKind, TokenUsage


class ReviewState(TypedDict, total=False):
    """State threaded through the review graph.

    ``total=False`` because state accumulates: ``diff`` does not exist until ``ingest`` has
    run, and requiring every key up front would mean seeding the dict with lies.
    """

    # -- set at entry --------------------------------------------------------
    run_id: RunId
    repo: RepoRef
    pr_number: int
    commit_sha: str

    # -- ingest --------------------------------------------------------------
    pull_request: PullRequest
    diff: Diff
    diff_excerpt: str
    symbols: list[str]
    paths: list[str]
    scoping_reduction_pct: float

    # -- route ---------------------------------------------------------------
    routing: RoutingDecision

    # -- specialists ---------------------------------------------------------
    candidates: list[CandidateFinding]
    corpus: dict[ChunkId, Chunk]
    # The per-specialist visibility map that cite-or-drop needs. Built here, in the graph,
    # from what retrieval actually returned to each specialist -- never from the whole corpus.
    visible: dict[SpecialistKind, list[ChunkId]]
    failed_specialists: list[str]

    # -- synthesis -----------------------------------------------------------
    findings: list[Finding]
    dropped: list[str]

    # -- accounting ----------------------------------------------------------
    usage: list[TokenUsage]
    error: str
