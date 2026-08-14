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
