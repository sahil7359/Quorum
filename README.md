# Quorum

> **Status: in development.** Every number below marked `TODO` has not been measured yet.
> Nothing here is a placeholder for a number I expect to get — it is a slot that stays
> empty until a reproducible run fills it.

A supervisor agent that dispatches specialist reviewer agents across a pull request,
grounds every finding in the repository's own documentation via citation-backed retrieval,
and requires human approval before anything is posted back to GitHub.

**Live demo:** `TODO: not yet deployed`

---

## What it does

1. Reads a pull request through the **official GitHub MCP server**.
2. A **supervisor** decides which specialists the diff warrants — and logs why.
3. Three specialists review it: **correctness**, **security**, **test-coverage**. Each
   grounds its findings in retrieved chunks of the target repository's own documentation.
4. Synthesis deduplicates and **drops any finding without a resolvable citation**.
5. The graph **stops** and waits for a human to approve, edit, or reject each finding.
6. Only then does anything reach GitHub — and an append-only audit row records it.
7. The whole capability is **published as an MCP server**, so any MCP client can call it.

## Why it exists

It demonstrates three things my previous project ([DataChat](https://github.com/)) does not:
**MCP consumed *and* published**, **multi-agent orchestration**, and **document RAG with
chunk-level citations**. Security is a deliberate *baseline* here, not the showcase —
DataChat already carries that story.

## Architecture

`TODO: diagram — Phase 13`

Four layers with dependencies pointing inward, enforced by `import-linter` **and** an AST
fitness test, not by convention. See [`docs/adr/0001-clean-architecture.md`](docs/adr/0001-clean-architecture.md).

```
app/
  domain/          entities, value objects, Protocol ports — stdlib imports only
  application/     agents (supervisor + specialists), services, use cases
  infrastructure/  mcp/, retrieval/, llm/, persistence/, observability/
  interface/       api/, schemas/, container.py (the one composition root)
```

## Measured numbers

| Number | Value | How measured | What it does not prove |
| --- | --- | --- | --- |
| NDCG@5 (rerank on vs off) | `TODO` | Phase 3 retrieval eval | — |
| Finding precision | `TODO` | Phase 6, vs human review comments on merged PRs | — |
| Finding recall | `TODO` | Phase 6 | Measured against an imperfect ceiling — human reviewers miss things too |
| Specialist routing accuracy | `TODO` | Phase 6 | — |
| Cost per review | `TODO` | Trace aggregation | — |
| Monthly cost | `TODO` | Provider dashboards | — |

## Known limitations

Filled in properly at Phase 13. Three that are true by design and will not change:

- **Quorum cannot report a defect the repository's documentation does not speak to.**
  Cite-or-drop means the failure mode of a grounded reviewer is *silence*. That is the
  intended failure mode, and it means recall will never be high.
- **A citation proves grounding, not aptness.** A finding can cite a real chunk that does
  not actually support it. Retrieval eval bounds this; nothing eliminates it.
- **Live reviews are rate-limited to a handful per day** by the free-tier token budget.
  The gallery is cached by commit SHA and always available.

## Documentation

| Document | What it answers |
| --- | --- |
| [PRD](docs/PRD.md) | What problem, for whom, what is out of scope |
| [Design](docs/Design.md) | Architecture, the agent graph, why a supervisor |
| [TechSpec](docs/TechSpec.md) | Stack and why, ports, MCP contracts, model routing |
| [AppFlow](docs/AppFlow.md) | One review end to end |
| [Schema](docs/Schema.md) | Data model and chunk identity |
| [Guardrails](docs/Guardrails.md) | Trust boundaries and 15 controls mapped to tests |
| [Security](docs/Security.md) | Threat model, OWASP mapping |
| [Rules](docs/Rules.md) | Engineering rules and their enforcement |
| [Tracker](docs/Tracker.md) | Current status |
| [`learn/`](learn/) | My build notes — the reasoning, phase by phase |
| [`docs/adr/`](docs/adr/) | Architecture decision records |

## Development

```bash
uv sync --all-extras
uv run pytest
uv run mypy app eval tests
uv run lint-imports
uv run ruff check .
```

## Licence

`TODO`
