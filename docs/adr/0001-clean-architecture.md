# ADR-0001 — Clean architecture with enforced layer boundaries

- **Status:** Accepted
- **Date:** 2026-08-14
- **Phase:** 0

## Context

Quorum has four kinds of moving part that change at different rates and for different
reasons: business concepts (a finding, a citation, an approval), orchestration (which
specialist runs when), integrations (GitHub MCP, Groq, pgvector, fastembed), and delivery
(HTTP, SSE, MCP server). Left alone, these tangle — the classic outcome is an agent module
that imports `httpx` and a database session, at which point nothing can be unit-tested
without a network and a Postgres.

There is a second, project-specific reason. This repository is interview material. "I used
clean architecture" is a claim anyone can make. "Here is the CI job that fails when a layer
boundary is crossed, and here is the commit where I proved it fails" is a demonstration.
The enforcement is as much the deliverable as the structure.

## Decision

Four layers, dependencies pointing inward, enforced by tooling:

```
interface        FastAPI routers, schemas, composition root
   │
   ├── application        agents, services, use cases   ──┐
   │                                                       ├── siblings, mutually forbidden
   └── infrastructure     MCP, retrieval, LLM, DB, obs   ──┘
                     │
                  domain        entities, value objects, Protocol ports
```

1. `domain` imports **only the standard library**. No framework, no driver, no client.
2. `application` and `infrastructure` both depend on `domain` and are **forbidden from
   importing each other**. Infrastructure implements domain ports; application consumes
   domain ports. Neither needs to know the other exists.
3. `interface` may import all three. It contains `container.py`, the single composition
   root, and it is the only place a concrete adapter is bound to a port.
4. Contracts are `typing.Protocol` — structural, so an adapter satisfies a port without
   importing anything from `domain` to inherit from.
5. Injection is by constructor. No service locator, no globals, no import-time side effects.

**Enforcement, in three independent mechanisms:**

- `import-linter` **layers** contract for the ordering, with `application` and
  `infrastructure` declared as independent siblings.
- `import-linter` **forbidden** contracts naming the specific packages `domain` (and, more
  loosely, `application`) may not import.
- An **AST fitness test** that parses every file under `app/domain`, walks its `Import` and
  `ImportFrom` nodes, and asserts each target is stdlib or `app.domain`.

The third exists because the first two are configuration, and configuration can be edited
in the same commit that violates it. The AST test encodes the rule as a test, so weakening
it is visible as weakening a test.

## Alternatives considered

**A single flat package with disciplined naming.** Fastest to write and perfectly adequate
for a project this size. Rejected because the boundary is the thing being demonstrated, and
because in practice "disciplined naming" degrades the first time something is urgent. The
cost of this rejection is real: more files, more indirection, and a reader has to follow a
port to an adapter to see what actually happens.

**Hexagonal with a single `core` and `adapters` split (two layers, not four).** Genuinely
tempting — it captures 80% of the benefit at half the ceremony, and the domain/application
split is the one people most often get wrong. Rejected because separating *what a finding is*
from *how a review is orchestrated* is exactly what lets the domain stay stdlib-only; merging
them would drag LangGraph into the pure layer and lose the AST fitness test entirely.

**Layers as convention, documented in a CONTRIBUTING file, unenforced.** Rejected outright.
An unenforced boundary is a boundary that has already been crossed; you just do not know
where yet.

**Putting the LangGraph graph in `infrastructure` to keep `application` framework-free.**
This is the purist position and I did not take it — see ADR-0002 for the reasoning and what
it costs.

## Consequences

**Good**

- Domain and application are testable with no network, no database, no model provider.
- Swapping Groq for Ollama, or pgvector for an in-memory store, is a constructor argument.
  This is what makes "eval runs locally, prod runs on Groq" a config change rather than a
  code change.
- The boundary is machine-checked in CI, so the claim is verifiable by a stranger.

**Bad, and accepted**

- More indirection. Reading "how does a review actually reach GitHub" means following
  `CodeHostPort` from application to `GitHubMcpClient` in infrastructure.
- `interface/container.py` becomes the one genuinely complex module — every wiring decision
  lands there. That is the deliberate trade: complexity concentrated in one visible place
  rather than smeared across the codebase.
- Some ports will have exactly one real adapter plus a fake. That is not waste — the fake is
  what makes the unit suite fast — but it does look like ceremony in isolation.

## Invariant and test

> **Invariant:** no module under `app/domain` imports anything outside the standard library
> and `app.domain` itself.

Enforced by `tests/architecture/test_domain_is_pure.py` and the `import-linter` contracts in
`pyproject.toml`. Proven to fail before being committed by temporarily adding
`import httpx` to a domain module — recorded in `HANDOFF.md`, Phase 0.
