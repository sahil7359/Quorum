<div align="center">

# Quorum

**A supervisor agent that reviews pull requests, grounds every finding in the target
repository's own documentation, and won't post anything without a human approving it first.**

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.141-009688?logo=fastapi&logoColor=white)
![LangGraph](https://img.shields.io/badge/LangGraph-1.2-1C3C3C)
![Postgres](https://img.shields.io/badge/Postgres-pgvector-4169E1?logo=postgresql&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-client%20%2B%20server-6E56CF)
![License](https://img.shields.io/badge/license-MIT-blue)

**Live demo:** not yet deployed — see [`learn/LLD.md`](learn/LLD.md) for what's built and
tested today versus what's left.

</div>

---

## The problem, and why grounding is the whole point

Most "AI code review" tools read a diff and write plausible-sounding comments you have to take
on faith. Quorum's bet is narrower: a finding is only as good as its citation, and citations
are checkable. A specialist proposes a finding with an untyped, optional `chunk_id: str | None`
— nothing trusts it. Exactly one function turns that into a real `Finding`, and it fails to,
routinely, in four distinct ways: no citation given, a citation that doesn't parse, a citation
for a chunk that was never retrieved, or — the subtle one — a citation for a chunk that's
completely real but wasn't shown to *that specialist*. A finding that survives all four checks
is grounded. One that doesn't is dropped before a human ever sees it.

## What it does

1. Reads a pull request through the **official GitHub MCP server**.
2. A **supervisor** decides which specialists the diff warrants — deterministic heuristics
   compute a floor, an LLM may only extend it, and the decision is logged with its reason.
3. Three specialists review it — **correctness**, **security**, **test-coverage** — each
   grounding its findings in retrieved chunks of the target repository's own documentation.
4. Synthesis deduplicates and **drops any finding without a resolvable citation**.
5. The graph **stops** and waits for a human to approve, edit, or reject each finding,
   durably — the process can die between the stop and the resume and it still works.
6. Only then does anything reach GitHub, and an append-only audit row records it.
7. The same capability is published as an **MCP server**, so any MCP client can call it —
   read-only, structurally: the object holding the write path is never even constructed.

## Architecture

```
GitHub PR ──► GitHub MCP Client ──► ingest → route → specialists → synthesise ──► cite-or-drop
                                       │         │         │              │
                                  diff cap   heuristic  hybrid retrieval  dedupe + rank
                                  + AST      floor +    (dense + BM25,   (no LLM call —
                                  scoping    LLM extend  RRF fusion)      comparison sort
                                                                          already orders it)
                                                                              │
                                                            ┌─────────────────▼──────────────┐
                                                            │  Approval gate (durable          │
                                                            │  interrupt, survives restarts)   │
                                                            │  → Publish (only write path,     │
                                                            │  re-checks audit log at write     │
                                                            │  time, not just at routing time)  │
                                                            └───────────────────────────────────┘

Serving: FastAPI + SSE   ·   idempotency-key coalescing   ·   review cache (zero-token replay)
         daily token budget (summed from fact, not decremented)   ·   live-review rate limit
```

Full diagrams, request lifecycle, and every table's schema: [`learn/HLD.md`](learn/HLD.md)
(architecture) and [`learn/LLD.md`](learn/LLD.md) (class-level design, algorithms, sequence
flows).

Four layers, dependencies pointing inward, enforced by `import-linter` **and** an AST fitness
test — not by convention. `domain` has zero framework imports; a fake satisfying its `Protocol`
ports is checked structurally against the real contract, so it can't silently drift.

```
app/
  domain/          entities, value objects, Protocol ports — stdlib imports only
  application/     the LangGraph pipeline: routing heuristics, specialist prompts, grounding
  infrastructure/  GitHub MCP client + server, LLM adapters (Ollama/Groq), Postgres/SQLite,
                   the hybrid retrieval stack
  interface/       the composition root — FastAPI app, ReviewService, wiring
```

## Measured numbers

Every number here is reproducible from a committed baseline and a gate that fails if it
regresses — see [`LEARN.md`](LEARN.md) for the story behind each one.

| Number | Value | How measured |
| --- | --- | --- |
| Retrieval NDCG@5 (hybrid dense+BM25) | 0.526 | `eval/retrieval/`, committed baseline, CI-gated |
| Reranking delta | **−0.079 NDCG@5 at 63–91× latency → cut** | Same eval; own corpus and labels, so the *delta* is trusted more than the absolute score |
| AST context-scoping token reduction | **34.86%** | Measured across real commits from this repo's own history |
| Trajectory-eval finding recall | **0%**, on a 10-PR real-world golden set | The model omits citations on real-world-sized diffs more often than a single hand-written smoke test suggested — written up honestly, not hidden |
| Trajectory-eval routing recall | 100% (trivially — correctness is unconditional) | Same eval |
| Citation rate | 1.00 by construction | A `Finding` cannot exist without a citation; the type system guarantees it |

**The finding-recall number is the one I'd lead with in an interview, not bury.** It's the
result of measuring honestly instead of shipping a plausible-looking metric nobody checked —
see [`learn/interview-prep.md`](learn/interview-prep.md) for the full "what does this actually
mean" conversation, including what I'd do differently.

## Known limitations

- **Quorum cannot report a defect the repository's documentation doesn't speak to.** Cite-or-drop
  means the failure mode of a grounded reviewer is *silence*, on purpose — that's why finding
  recall will never be high, and why the number above is reported plainly rather than chased.
- **A citation proves grounding, not aptness.** A finding can cite a real, visible chunk that
  doesn't actually support its claim. The retrieval eval bounds how often the wrong chunk comes
  back; nothing eliminates a right chunk being cited for the wrong reason.
- **Single-operator trust model.** One shared token budget, one rate limit, no per-tenant
  isolation. Extending to multi-tenant is additive to the schema, not a rewrite — see
  [`learn/interview-prep.md`](learn/interview-prep.md) for exactly what that would take.
- **Not load-tested.** The design supports horizontal scaling (stateless orchestrator, durable
  checkpointing, ports-and-adapters persistence) but I haven't proven it under real concurrency.

## Documentation

| Document | What it answers |
| --- | --- |
| [`learn/HLD.md`](learn/HLD.md) | System architecture, request lifecycle, scaling posture |
| [`learn/LLD.md`](learn/LLD.md) | Class-level design, algorithms, schemas, sequence flows |
| [`learn/interview-prep.md`](learn/interview-prep.md) | System-design Q&A: cost control, scaling, honest gaps |
| [`LEARN.md`](LEARN.md) | Change log with reasoning — what changed, why, what I'd reconsider |
| [PRD](docs/PRD.md) | What problem, for whom, what is out of scope |
| [Design](docs/Design.md) | The agent graph, why a supervisor, the log/trace/audit split |
| [TechSpec](docs/TechSpec.md) | Stack and why, ports, MCP contracts, model routing |
| [AppFlow](docs/AppFlow.md) | One review end to end, with a failure-path table |
| [Schema](docs/Schema.md) | Data model and chunk identity |
| [Guardrails](docs/Guardrails.md) | Trust boundaries and every control mapped to a test |
| [Security](docs/Security.md) | Threat model, OWASP LLM + Agentic Top 10 mapping |
| [`docs/adr/`](docs/adr/) | Architecture decision records |

## Development

```bash
uv sync --all-extras
uv run pytest
uv run mypy app eval tests
uv run lint-imports
uv run ruff check .
```

Needs Docker (Postgres + pgvector for integration tests) and, for a live model, either a local
Ollama instance or a Groq API key — see [`.env.example`](.env.example).

## License

MIT — see [LICENSE](LICENSE).
