# Quorum — Implementation Plan

Fourteen phases. Each ends in three things: **a commit, a passing test suite, and a
`learn/NN-<topic>.md` note** — plus `CHANGELOG.md`, `docs/INTERVIEW_BRIEF.md`, and any ADRs
the phase earned. A phase is not complete until all four records are updated.

---

## Phase 0 — Scaffolding · `learn/00-scaffolding.md`

**Deliverables:** `uv` project on Python 3.12; ruff + mypy strict config; pytest layout;
pre-commit with gitleaks; import-linter contracts; CI skeleton; `.claude/` gitignored from
the first commit; typed `Settings` via pydantic-settings.

**Tests:** `tests/architecture/test_layers.py` (import-linter contracts execute in pytest,
not only in CI); `test_settings_loads_from_env`; a deliberate boundary violation proves the
contract fails.

**Gate proof:** add a temporary `import httpx` to `app/domain`, watch import-linter and the
AST test go red, remove it.

---

## Phase 1 — Domain core · `learn/01-domain-and-ports.md`

**Deliverables:** entities (`PullRequest`, `Diff`, `DiffHunk`, `Chunk`, `Finding`,
`Citation`, `Review`, `Approval`, `RunId`); value objects (`ChunkId`, `Severity`,
`SpecialistKind`, `ApprovalAction`, `TokenUsage`); every `Protocol` port from TechSpec §2.
Frozen dataclasses. Zero infrastructure imports.

**Tests:** AST fitness test walking `app/domain/**/*.py` asserting every import is stdlib or
`app.domain`; value-object invariants (severity ordering, chunk-id derivation and stability);
`test_ports_are_protocols`.

**Gate proof:** add `import sqlalchemy` to a domain module, watch the AST test fail, remove.

---

## Phase 2 — MCP client · `learn/02-mcp-client.md`

**Deliverables:** `GitHubMcpClient` over the official GitHub MCP server via stdio; tool
allowlist; typed responses parsed into domain entities; retry with backoff; write tools
guarded behind an approval token.

**Tests:** integration tests against a **fake MCP server implemented with the real `mcp`
SDK over real stdio** — real protocol, no network, no token. Allowlist refusal;
write-without-approval refusal; token-not-in-argv.

**Gate proof:** remove the allowlist check, watch `test_non_allowlisted_tool_is_refused` fail.

---

## Phase 3 — Document RAG · `learn/03-retrieval.md` ⚠ **highest-risk phase**

**Deliverables:** markdown+AST chunker with **chunk-level ids traceable to
`(file, section, offset)`**; `fastembed` dense embedder; hand-rolled code-aware BM25; RRF
fusion; cross-encoder rerank behind a flag; chunk store (pgvector + in-memory);
ingest CLI for the gallery repos.

**Retrieval eval, built alongside — not after:** NDCG@5 and Recall@5 **with and without**
reranking, reported as a delta. *If reranking does not earn its latency, cut it and write
that up as a finding.*

**Tests:** the five chunk-id invariants from `Schema.md` §2.1; BM25 tokenizer splits
`camelCase`/`snake_case`; RRF ordering; `test_retrieval_eval_runs`.

**Why this is the risky one:** the chunk-id scheme fixes what every specialist in Phase 4
builds on. File-level ids would invalidate every retrieval number published. Flagged
prominently in `HANDOFF.md`.

---

## Phase 4 — Specialists + supervisor · `learn/04-multi-agent.md`

**Deliverables:** LangGraph graph (`ingest → route → specialists → synthesise`); heuristic
routing floor + LLM extension; three specialists; AST-scoped context per changed region;
cite-or-drop enforced at synthesis; traced base node.

**Reported number:** token reduction from AST-scoped context vs. a whole-file baseline.

**Tests:** `test_every_node_is_traced`; `test_uncited_finding_is_dropped`;
`test_hallucinated_chunk_id_is_dropped`; `test_correctness_always_routed`;
`test_malformed_specialist_output_is_dropped`; routing heuristics table-driven.

---

## Phase 5 — HITL + audit · `learn/05-hitl-and-audit.md`

**Deliverables:** durable `interrupt()` with a Postgres/SQLite checkpointer; approval
service; append-only audit table with DB-level rules; publish guard requiring a matching
`approved` row and `payload_hash`.

**Tests:** `test_publish_requires_approval_row`; `test_edited_finding_requires_reapproval`;
`test_audit_is_append_only` (attempt UPDATE and DELETE, assert no effect);
`test_interrupt_survives_process_restart` (resume from a fresh checkpointer instance).

**Gate proof:** delete the publish guard, watch the test fail.

---

## Phase 6 — Trajectory eval · `learn/06-trajectory-eval.md`

**Deliverables:** golden set built from **merged PRs carrying real human review comments**;
metrics — finding precision/recall, tool-call correctness, specialist routing accuracy,
steps and cost per review; two-tier gate against committed per-provider baselines.

**Honesty requirement:** `learn/06` must state that human reviewers miss things, so recall
is measured against an **imperfect ceiling**. If the eval cannot execute (no key, Ollama
down, too few labelled PRs), ship the harness, write `TODO: not yet run`, and say so
plainly in `HANDOFF.md`.

**Tests:** metric functions unit-tested against hand-computed fixtures; a mutation test that
the gate **fails** when a baseline is regressed.

---

## Phase 7 — MCP server · `learn/07-mcp-server.md`

**Deliverables:** Quorum published as an MCP server (stdio + streamable HTTP); tools
`review_pull_request`, `get_review`, `list_ingested_repos`, `get_chunk`; published schema;
`README` section on connecting a client.

**Non-negotiable:** the MCP surface has **no write path**. An MCP client gets findings, not
side effects, or the approval gate could be bypassed.

**Tests:** `test_mcp_server_has_no_write_path`; schema round-trip; a real MCP client session
against our own server over stdio.

---

## Phase 8 — Serving · `learn/08-serving.md`

FastAPI, SSE streaming the review as it forms, idempotency keys, rate limiting, health and
readiness probes.

---

## Phase 9 — Security baseline · `learn/09-security-baseline.md`

Implement and test every control in `Guardrails.md` §2 not already covered. Map OWASP LLM +
Agentic Top 10 to passing tests. **Then stop** — this is a baseline, not a showcase.

---

## Phase 10 — Observability · `learn/10-observability.md`

Structured logging, tracing and audit as three distinct mechanisms; per-agent spans; tokens,
cost, latency. A review must be reconstructable end to end from logs alone, and the approval
trail must survive log rotation. The learn note must explain the log/trace/audit distinction
in my own words.

---

## Phase 11 — CI · `learn/11-ci.md`

Full suite, import-linter, `mypy --strict`, retrieval eval, trajectory gate, container build,
Postgres service container.

---

## Phase 12 — Demo · `learn/12-demo.md`

Gallery of six pre-selected PRs cached by SHA; one rate-limited live button; GitHub App on
my own repositories for the write path.

---

## Phase 13 — Deploy and hand-back · `learn/13-deploy.md`

Live URL, README with architecture diagram, demo GIF, measured numbers, honest known
limitations and failure modes.

---

# Reflection — re-read of this plan before writing any code

*Written immediately after drafting the documents above, reading them critically. These are
changes I would make on reflection; the ones I acted on are marked.*

**R1. Phase 3 is too big, and it is also the riskiest. — acted on.**
Chunker + embedder + BM25 + fusion + rerank + store + ingest CLI + a full retrieval eval is
not one sitting. I am splitting it internally into **3a (chunking and chunk identity, with
its invariant tests)** and **3b (retrieval, fusion, rerank, eval)**, committed separately.
3a is the part that cannot be corrected later, so it gets its own commit and its own review
before anything depends on it.

**R2. The retrieval eval labels are mine, and that is a weakness. — acted on in the record.**
Phase 3's golden set is queries and relevance judgements I write myself against docs I chose.
That is grading my own homework, exactly what the trajectory eval was designed to avoid.
It is still worth having (it measures the rerank delta, which is a *relative* comparison and
far more robust to label bias than an absolute score). `learn/03` and `INTERVIEW_BRIEF` must
say this plainly: **the rerank delta is the trustworthy number; the absolute NDCG is not.**

**R3. `application` importing LangGraph weakens the layer story. — accepted, documented.**
A purist puts the graph in infrastructure and leaves application framework-free. I keep
LangGraph in application because the graph *is* the application logic, and moving it would
create a fake port around an orchestration engine. This is a real trade and ADR-0002 records
it rather than pretending it is clean.

**R4. Sequential specialists is the right default but I should not hardcode it. — acted on.**
The 12K tokens/min ceiling is a *Groq free tier* fact, not a *design* fact. Local Ollama has
no such limit, so eval runs would be needlessly slow. Concurrency becomes a config knob,
defaulted off, with the reason in the config comment.

**R5. `Phase 6 depends on data I may not be able to get.** — flagged.**
The golden set needs merged PRs with substantive human review comments. Unauthenticated
GitHub API allows 60 requests/hour. If I cannot assemble enough labelled PRs, the harness
ships with `TODO: not yet run` and `HANDOFF.md` says so. **Under no circumstances is a
plausible-looking number written for a run that did not happen.**

**R6. I under-specified what happens when retrieval returns nothing relevant. — acted on.**
`AppFlow.md` now says it explicitly: the specialist produces no findings, and cite-or-drop
guarantees it cannot invent one. The failure mode of a grounded reviewer is *silence*, and
silence is the correct failure mode. This belongs in the README's limitations section, not
buried in a flow doc.

**R7. Six gallery repos is stated everywhere but never chosen. — deferred to Phase 12,
deliberately.** They must be repositories with (a) real documentation worth retrieving
against and (b) merged PRs carrying substantive review comments. Picking them before the
retrieval and eval code exists risks choosing repos that flatter the system. Choosing them
*after* the metrics exist is more honest, and the selection criteria get written down first.

**R8. `config_hash` needs a precise definition or the cache silently serves stale reviews.
— acted on.** It hashes prompt template versions, model ids, retrieval settings (k,
candidates, rerank on/off), and the chunker version. Anything that changes the output of a
review must be in it. A cache key that misses a prompt change is worse than no cache,
because it is confidently wrong.
