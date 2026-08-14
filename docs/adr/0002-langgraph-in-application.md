# ADR-0002 — LangGraph lives in the application layer

- **Status:** Accepted
- **Date:** 2026-08-14
- **Phase:** 0

## Context

ADR-0001 establishes that `domain` imports only the standard library and that
`application` depends on `domain` alone. The obvious follow-up question is where LangGraph
goes. It is unambiguously a third-party framework, and the agents that use it —
supervisor, three specialists, the graph wiring — are unambiguously application logic.

The strict clean-architecture answer is that a framework belongs in `infrastructure`, with
`application` depending on an abstraction. Applied here that means defining something like
an `OrchestratorPort` in `domain`, implementing it with LangGraph in `infrastructure`, and
having application code describe a graph in our own vocabulary that the adapter translates.

I do not think that is worth it, and this ADR is where I say why rather than quietly
letting the import sit there.

## Decision

**`app.application` may import `langgraph`. `app.domain` may not.**

The import-linter "Application is framework-light" contract lists `httpx`, `sqlalchemy`,
`psycopg`, `mcp`, `fastembed`, and `fastapi` as forbidden — and deliberately omits
`langgraph`. The omission is the decision, so it is recorded here rather than inferred from
a config file.

Everything LangGraph touches is still injected through domain ports: nodes receive a
`ChatModelPort`, a `RetrieverPort`, a `CodeHostPort`. LangGraph provides control flow and
checkpointing. It does not provide I/O, and no node instantiates an adapter.

## Alternatives considered

**An `OrchestratorPort` in `domain`, LangGraph adapter in `infrastructure`.** The pure
option. Rejected for three reasons. First, the port would have to expose graph
construction, conditional edges, state reducers, checkpointing, *and* `interrupt()` — at
which point it is not an abstraction, it is a transcription of LangGraph's API with our
names on it, and swapping the implementation would still be a rewrite. Second, durable
`interrupt()` is the single feature that made me choose LangGraph; wrapping it in a port
that only LangGraph can satisfy is ceremony that buys nothing. Third, it would put the most
subtle logic in the project — the human-approval pause — behind an indirection, which makes
the thing I most want a reader to understand harder to read.

**Hand-roll the orchestration: a state machine, explicit nodes, our own checkpointing to
Postgres.** Genuinely tempting, and it would make `application` framework-free for real. I
rejected it because durable resumption is the hard part and I would be reimplementing it
badly. A review proposed at 14:00 and approved at 19:00 must resume in a different process
after a free-tier instance has slept. That is checkpoint serialisation, state versioning,
and replay semantics — a project in itself, and not the project I am demonstrating.

**Put the whole agent package in `infrastructure`.** Rejected because it inverts the
meaning of the layers: routing which specialist reviews which diff is the core use case,
not an integration detail. If that lives in infrastructure, `application` is empty and the
architecture is decorative.

## Consequences

**Good**

- The graph reads like the design document. `ingest → route → specialists → synthesise →
  interrupt → publish` is visible in one file.
- Durable `interrupt()` is used directly, which is why the HITL gate survives a process
  restart.
- No fake abstraction that would have to be broken the first time LangGraph does something
  interesting.

**Bad, and accepted**

- `application` is not framework-free, so the layer story has an asterisk. When asked "is
  your application layer pure?" the honest answer is "it depends on one framework, on
  purpose, and here is the ADR" — not "yes".
- Replacing LangGraph would be an application-layer rewrite, not an adapter swap. I accept
  that because a framework migration is a real project either way, and the port would not
  have made it cheap — only made it look cheap.
- The boundary is now a judgement rather than a rule, which means it can erode. Mitigation:
  the forbidden contract still blocks `httpx`, `sqlalchemy`, `mcp`, `fastembed`, `psycopg`
  and `fastapi` in `application`, so LangGraph is a named exception rather than an open door.

## Invariant and test

> **Invariant:** `app.application` performs no I/O of its own — no HTTP client, no database
> driver, no MCP client, no embedding model. All I/O arrives through a domain port.

Enforced by the `Application is framework-light` import-linter contract and by
`tests/architecture/test_application_has_no_io.py`.
