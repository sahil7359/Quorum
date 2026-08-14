# Changelog

Chronological, grouped by phase. One line per change, plain language: what changed and what
it affects. This is the fast index for finding where something lives.

---

## Planning — 2026-08-14

- Added `docs/PRD.md` — problem, users, scope, non-goals; states explicitly that security is
  a baseline here and not the showcase.
- Added `docs/Design.md` — architecture, agent graph, supervisor routing, retrieval design,
  HITL/audit design, and the three-way log/trace/audit split.
- Added `docs/TechSpec.md` — stack with the rejected alternative for each choice, port table,
  MCP tool contracts for both client and server, model routing, cost controls.
- Added `docs/AppFlow.md` — one review end to end, from request to posted comment, plus a
  failure-path table.
- Added `docs/Schema.md` — data model; chunk identity scheme and its five invariants; the
  append-only audit table.
- Added `docs/ImplementationPlan.md` — the 14 phases with deliverables, tests, and learn
  notes, followed by a critical re-read with eight changes made on reflection.
- Added `docs/Rules.md` — engineering rules with the enforcement mechanism named for each.
- Added `docs/Guardrails.md` — trust boundaries and 15 numbered controls, each mapped to a test.
- Added `docs/Security.md` — threat model, token scoping, OWASP LLM + Agentic Top 10 mapping.
- Added `docs/Tracker.md` — status board, including environment facts established at start.
- Added `docs/adr/0001-clean-architecture.md` — four layers, enforced three ways.
- Added `.gitignore` with `.claude/` ignored from the first commit, and `.env.example`.

## Phase 0 — Scaffolding — 2026-08-14

- Added `pyproject.toml`: `uv` project on Python 3.12, ruff, mypy `--strict`, pytest, and
  six import-linter contracts. Affects every subsequent phase's tooling.
- Added `app/{domain,application,infrastructure,interface}/` package skeletons with the
  layering rule documented in each `__init__`.
- Added `app/infrastructure/config.py` — typed `Settings` read from `QUORUM_*` env vars,
  plus `config_hash()`, which defines the review cache key. Affects caching and cost control.
- Added `tests/architecture/` — AST fitness tests for domain purity and the application
  layer's no-I/O rule, plus a test that runs the import-linter contracts inside pytest.
- Added `tests/support/ast_imports.py` — shared AST import extraction that parses rather
  than imports, so a boundary violation is detected without executing it.
- Added `tests/unit/test_config.py` — cache-key behaviour, both positive and negative:
  output-affecting settings must change the hash, secrets and URLs must not.
- Added `.pre-commit-config.yaml` and `.gitleaks.toml` — gitleaks, ruff, mypy, import-linter.
- Added `.github/workflows/ci.yml` — quality, secrets and test jobs. Eval gates land in Phase 11.
- Added `.gitattributes` (LF normalisation) and `.python-version` (3.12 pin).
- Added `docs/adr/0002-langgraph-in-application.md` — LangGraph is a named exception in the
  application layer; the graph is the application logic. Affects Phase 4.
- Added `learn/00-scaffolding.md`.

## Phase 1 — Domain core — 2026-08-14

- Added `app/domain/values.py` — `ChunkId`/`ChunkLocator` (chunk-level identity derived from
  repo, commit, file, section and byte offsets), `Severity` with an explicit `rank` because
  alphabetical order is backwards, `SpecialistKind` as a closed three-member set, `RunId`,
  `FindingId`, `TokenUsage`, `BudgetState`. Affects retrieval, citations and budgeting.
- Added `app/domain/entities.py` — the `CandidateFinding` / `Finding` split that makes
  cite-or-drop a type transition, plus `Diff`, `ChangedFile`, `PullRequest`, `RepoRef`,
  `Chunk`, `Citation`, `RoutingDecision`, `Review`, `Approval`, `AuditEvent`, `CacheKey`.
- Added `app/domain/grounding.py` — `ground_candidates()` (cite-or-drop with four itemised
  drop reasons and per-specialist visibility) and `deduplicate()`. Affects Phase 4 synthesis.
- Added `app/domain/ports.py` — 11 Protocol ports. `CodeHostPort` write methods take an
  `Approval` as a required argument, so an unauthorised write is inexpressible.
- Added `app/domain/errors.py` — the failures the design anticipates, including
  `SpecialistFailedError` (handled) vs `AllSpecialistsFailedError` (fatal).
- Added `tests/unit/test_values.py`, `test_entities.py`, `test_grounding.py` and
  `tests/architecture/test_ports_are_protocols.py` — 133 tests total.
- Fixed `docs/Schema.md` — the chunk-id invariant said ids "round trip" to a locator. They
  do not; a hash is not reversible. Reworded to *verifiable against*, test renamed to
  `test_chunk_id_verifies_against_its_locator`.
- Added `learn/01-domain-and-ports.md`.

## Phase 2 — MCP client — 2026-08-14

- Added `app/infrastructure/mcp/allowlist.py` — the vetted tool set, split into read and
  write. Affects everything the agent can reach on GitHub.
- Added `app/infrastructure/mcp/github_client.py` — `GitHubMcpClient` over stdio, satisfying
  `CodeHostPort`. Allowlist and write-authorisation guards live here rather than in the
  graph, so they hold for every caller. Token passed by environment, never argv.
- Added `app/infrastructure/mcp/diff_parser.py` — unified diff to `Diff`/`ChangedFile`/
  `DiffHunk`, with the size cap and a truncation flag that travels with the data.
- Added `tests/support/fake_github_mcp_server.py` — a real MCP server built with the real
  SDK, with four behaviour modes. Lets the protocol be exercised without a GitHub token.
- Added `tests/support/fakes.py` — `RecordingLogger`, `NullTracer`, `FrozenClock`.
- Added `tests/integration/test_github_mcp_client.py` (22 tests over real stdio),
  `tests/unit/test_diff_parser.py`, `tests/security/test_mcp_allowlist.py`. 196 total.
- **Fixed a real bug found by a failed gate proof:** the diff parser excluded lines starting
  with `+++` as "file headers". That exclusion was dead code (headers never reach the
  counter) *and* wrong (added content beginning with `++` was silently uncounted). Removed,
  and replaced with tests that exercise the real case.
- Added `docs/adr/0003-guards-live-in-the-adapter.md` and `learn/02-mcp-client.md`.
- Added dependency: `mcp>=1.9` (resolved to 2.0.0).

## Phase 3 — Document RAG — 2026-08-14

- Added `app/infrastructure/retrieval/chunker.py` — markdown chunking with chunk-level ids
  traceable to `(repo, commit, file, section, byte offsets)`, heading breadcrumbs, code-fence
  awareness, CRLF normalisation. Affects every citation and every retrieval number.
- Added `app/infrastructure/retrieval/sparse.py` — hand-rolled BM25 with a tokenizer that
  splits camelCase/snake_case identifiers while keeping the whole token. IDF clamped at zero.
- Added `app/infrastructure/retrieval/fusion.py` — reciprocal rank fusion, deterministic
  under ties.
- Added `app/infrastructure/retrieval/dense.py` — `FastEmbedEmbedder` (bge-small, 384d, ONNX),
  `HashEmbedder` for plumbing tests, `InMemoryChunkStore` (exhaustive scan, no index).
- Added `app/infrastructure/retrieval/hybrid.py` — `HybridRetriever` and `FastEmbedReranker`.
- Added `eval/retrieval/` — golden set (20 queries, section-level labels), metrics
  (NDCG@5, Recall@5, Success@5), runner, and a regression gate split into pure comparison
  logic plus a slow CLI.
- Added `eval/corpus/` — **frozen** snapshot of `docs/`, refreshed only via `--snapshot`.
- Added `eval/baselines/retrieval.json` — committed baseline from a real run.
- **Measured: cross-encoder reranking loses.** NDCG@5 −0.0793, Recall@5 −0.0708, 89× latency.
  Disabled by default. See `docs/adr/0004-rerank-disabled.md`.
- **Fixed a self-referential eval:** the corpus was the live `docs/` tree, so writing the ADR
  that recorded the result changed the result. Reports now carry a `corpus_sha` and the gate
  raises `CorpusMismatch` instead of reporting a false regression.
- Added `docs/adr/0004-rerank-disabled.md` and `learn/03-retrieval.md`.
- Added dependency: `fastembed`.
- Changed default `QUORUM_RERANK_ENABLED` to `false`.
