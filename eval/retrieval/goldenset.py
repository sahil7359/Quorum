"""The retrieval golden set.

**Read this before trusting any number produced from it.**

These relevance labels are mine. I wrote the queries, I chose the corpus, and I decided
which sections count as relevant. That is grading my own homework, and it is exactly the
methodology the trajectory eval (Phase 6) was designed to avoid by using real human review
comments as labels.

I kept it anyway, for one reason: the number this eval exists to produce is the **rerank
delta** — NDCG@5 with reranking minus NDCG@5 without. A delta is a *relative* comparison
between two configurations scored against the same labels, so consistent label bias largely
cancels. The absolute NDCG is far less trustworthy and should be read as "roughly this
ballpark on a corpus I wrote", not as a benchmark figure.

Labels are expressed as `(file_path, section_substring)` rather than chunk ids, so
re-chunking does not invalidate them. A chunk is relevant when its locator's file matches
and its section path contains the substring.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RelevanceTarget:
    file_path: str
    section_contains: str


@dataclass(frozen=True, slots=True)
class GoldenQuery:
    query_id: str
    query: str
    targets: tuple[RelevanceTarget, ...]
    note: str = ""


# Queries are phrased the way a specialist agent would phrase them: a concern plus the
# symbols it saw in the diff, not a full-sentence question.
GOLDEN_QUERIES: tuple[GoldenQuery, ...] = (
    GoldenQuery(
        "q01",
        "how are layer boundaries between domain and infrastructure enforced",
        (
            RelevanceTarget("docs/Rules.md", "Architecture"),
            RelevanceTarget("docs/adr/0001-clean-architecture.md", "Decision"),
        ),
    ),
    GoldenQuery(
        "q02",
        "ChunkId derivation chunk-level identity byte offsets",
        (RelevanceTarget("docs/Schema.md", "chunk"),),
        note="exact-identifier query: the case dense retrieval is weakest on",
    ),
    GoldenQuery(
        "q03",
        "what happens when a finding has no citation",
        (
            RelevanceTarget("docs/Design.md", "cite-or-drop"),
            RelevanceTarget("docs/Guardrails.md", "Controls"),
        ),
    ),
    GoldenQuery(
        "q04",
        "QUORUM_MAX_DIFF_LINES diff size cap truncation",
        (
            RelevanceTarget("docs/TechSpec.md", "Cost controls"),
            RelevanceTarget("docs/AppFlow.md", "ingest"),
        ),
        note="exact-identifier query",
    ),
    GoldenQuery(
        "q05",
        "human approval required before posting to GitHub",
        (
            RelevanceTarget("docs/Design.md", "Human-in-the-loop"),
            RelevanceTarget("docs/AppFlow.md", "publish"),
        ),
    ),
    GoldenQuery(
        "q06",
        "why a supervisor instead of one agent with three prompts",
        (RelevanceTarget("docs/Design.md", "supervisor"),),
    ),
    GoldenQuery(
        "q07",
        "append-only audit table survives log rotation",
        (
            RelevanceTarget("docs/Schema.md", "audit"),
            RelevanceTarget("docs/Design.md", "Observability"),
        ),
    ),
    GoldenQuery(
        "q08",
        "prompt injection untrusted diff content fencing",
        (
            RelevanceTarget("docs/Guardrails.md", "Trust boundaries"),
            RelevanceTarget("docs/Security.md", "Threat model"),
        ),
    ),
    GoldenQuery(
        "q09",
        "which specialists exist and when are they chosen",
        (
            RelevanceTarget("docs/Design.md", "routing"),
            RelevanceTarget("docs/PRD.md", "Scope"),
        ),
    ),
    GoldenQuery(
        "q10",
        "BM25 code-aware tokenizer camelCase snake_case identifiers",
        (
            RelevanceTarget("docs/Design.md", "Retrieval"),
            RelevanceTarget("docs/TechSpec.md", "Stack"),
        ),
        note="exact-identifier query",
    ),
    GoldenQuery(
        "q11",
        "daily token budget exhausted fallback behaviour",
        (
            RelevanceTarget("docs/TechSpec.md", "Cost controls"),
            RelevanceTarget("docs/AppFlow.md", "Budget"),
        ),
    ),
    GoldenQuery(
        "q12",
        "review cache key commit SHA config hash",
        (
            RelevanceTarget("docs/Design.md", "Cost control"),
            RelevanceTarget("docs/Schema.md", "review_cache"),
        ),
    ),
    GoldenQuery(
        "q13",
        "mypy strict typing rules for this repository",
        (RelevanceTarget("docs/Rules.md", "Typing"),),
    ),
    GoldenQuery(
        "q14",
        "what does the MCP server expose and can it write to GitHub",
        (RelevanceTarget("docs/TechSpec.md", "MCP"),),
    ),
    GoldenQuery(
        "q15",
        "scoped personal access token permissions",
        (RelevanceTarget("docs/Security.md", "Token scoping"),),
    ),
    GoldenQuery(
        "q16",
        "how is a test proven able to fail before being committed",
        (
            RelevanceTarget("docs/Rules.md", "Testing"),
            RelevanceTarget("docs/TechSpec.md", "enforcement"),
        ),
    ),
    GoldenQuery(
        "q17",
        "payload_hash binds approval to exact text",
        (
            RelevanceTarget("docs/Schema.md", "findings"),
            RelevanceTarget("docs/Guardrails.md", "Controls"),
        ),
        note="exact-identifier query",
    ),
    GoldenQuery(
        "q18",
        "specialists run sequentially rather than in parallel and why",
        (
            RelevanceTarget("docs/Design.md", "Cost control"),
            RelevanceTarget("docs/PRD.md", "Constraints"),
        ),
    ),
    GoldenQuery(
        "q19",
        "recall measured against an imperfect ceiling human reviewers miss things",
        (
            RelevanceTarget("docs/PRD.md", "Success metrics"),
            RelevanceTarget("docs/ImplementationPlan.md", "Trajectory eval"),
        ),
    ),
    GoldenQuery(
        "q20",
        "what is explicitly out of scope for version 1",
        (RelevanceTarget("docs/PRD.md", "Scope"),),
    ),
)


def relevant_chunk_ids(query: GoldenQuery, chunks: list[object]) -> set[str]:
    """Resolve section-level labels to the chunk ids of the current chunking."""
    matched: set[str] = set()
    for chunk in chunks:
        locator = chunk.locator  # type: ignore[attr-defined]
        for target in query.targets:
            if locator.file_path == target.file_path and (
                target.section_contains.lower() in locator.section_path.lower()
            ):
                matched.add(str(chunk.chunk_id))  # type: ignore[attr-defined]
    return matched
