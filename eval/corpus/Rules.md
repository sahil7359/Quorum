# Quorum — Engineering Rules

These are enforced, not aspirational. Where a rule has a mechanism, the mechanism is named.

## Architecture

1. **Dependencies point inward.** `domain` knows nothing. `application` and `infrastructure`
   know `domain` and **not each other**. `interface` knows all three and is the only place
   wiring happens.
   *Enforced by:* import-linter `layers` contract + `tests/architecture/test_layers.py`.
2. **`domain` imports nothing but the standard library.** No httpx, sqlalchemy, langgraph,
   structlog, mcp, fastembed, fastapi, psycopg.
   *Enforced by:* import-linter `forbidden` contract + an AST fitness test that parses every
   file under `app/domain` and inspects its import nodes.
3. **All cross-layer contracts are `typing.Protocol`** in `app/domain/ports`, structural not
   nominal, so adapters never import a base class from domain to satisfy it.
4. **Constructor injection only.** No service locator, no global container, no import-time
   side effects. The composition root is `app/interface/container.py`.
5. **New dependency = an ADR.** If I cannot explain in an interview what a dependency does
   and what it replaced, it does not go in.

## Typing

6. `mypy --strict` over `app/`, `eval/`, `tests/`. CI fails on any error.
7. `# type: ignore` must name the error code and carry a reason on the same line.
8. Public functions are fully annotated. `Any` in a signature needs a written justification.
9. Value objects are frozen dataclasses or `Literal`/`NewType` — not bare `str`. A
   `ChunkId` that is a `str` is a bug waiting to be swapped with a `FindingId`.

## Testing

10. **A test that cannot fail is worse than no test.** Before any gate or assertion is
    committed, break the thing it guards, watch it go red, restore. Record that in `HANDOFF.md`.
11. Test layout mirrors the layers: `tests/unit`, `tests/integration`, `tests/security`,
    `tests/architecture`.
12. Unit tests use fakes from `tests/fakes/`, never mocks of our own Protocols — a fake that
    implements the Protocol is checked by mypy; a `Mock` is not.
13. Integration tests that need Postgres skip cleanly when it is unavailable and are
    **not** silently skipped in CI — CI runs a service container, and a separate test
    asserts the suite actually exercised the real store.
14. Every invariant named in a design document has a named test. Design docs cite the test.

## Observability

15. **Telemetry never fails a request.** Every emit path swallows its own exceptions.
16. **Never log secrets, tokens, or raw diff content at INFO.** Redact before the call.
17. Every graph node is traced by the base class, never by the node remembering.
    *Enforced by:* `test_every_node_is_traced`.
18. **Log decisions, not events.** `chose security specialist` is nearly useless.
    `chose security specialist: diff touches app/auth/, 2 injection heuristics matched`
    is debuggable.
19. Levels mean what they say: ERROR = broke, needs a human. WARN = degraded but handled.
    INFO = state transitions and decisions, one line per graph node, reading as a narrative.
    DEBUG = payloads and prompts, off in production.

## Comments

20. **Every non-obvious decision carries a `# why:` comment naming the rejected alternative.**

    ```python
    # why: BM25 + dense because pure dense missed exact identifier matches.
    #      alt: dense-only (simpler, worse on symbols)
    ```

21. Comments explain *why*, never *what*. The code says what.

## Records — a phase is not done until all four are updated

22. `learn/NN-topic.md` — one per phase. First person, "my build notes". What I built, why,
    **what I rejected and why**. Diagrams and short excerpts where they make a decision
    click. Names the invariant each choice protects and the test enforcing it. Written in
    the same session as the code, never batched at the end.
23. `docs/adr/NNNN-slug.md` — one per significant decision. Context · Decision ·
    Alternatives considered · Consequences · Status. Written when the decision is made,
    never retrofitted.
24. `CHANGELOG.md` — chronological, grouped by phase, one line per change in plain language.
25. `docs/INTERVIEW_BRIEF.md` — appended every phase: five plausible interview questions with
    answers in my voice; the one thing most likely to be challenged and the honest response;
    every number produced, how it was measured, and what it does **not** prove.

**If a session runs short, cut scope from the code, never from the record.** A feature can
be finished later; reasoning cannot be reconstructed later.

## Metrics

26. **Never fabricate a number.** `TODO: not yet measured` beats a plausible figure. Every
    published number traces to a run that can be reproduced.
27. Every number ships with what it does **not** prove.
28. Baselines are committed. A gate that compares against a value computed in the same run
    is a tautology, not a gate.

## Git

29. Work in pull requests, never direct to `main`. One branch per phase.
30. **I write every commit message.** The agent reports what changed; it does not compose
    the message.
31. `.claude/` is gitignored from the first commit.
32. `gitleaks` runs in pre-commit and in CI.

## Scope

33. One phase per session. No half-finished refactors.
34. If a feature moves neither a Definition-of-Done item nor a published number, it is out.
35. Three specialists. A fourth requires eval evidence that it earns its cost.
36. Keep the agent simple and reliable. A sprawling multi-agent demo is a liability.
