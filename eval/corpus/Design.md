# Quorum — Design

## 1. Shape of the system

```
                    ┌──────────────────────────────────────────┐
   MCP clients ────►│  Quorum MCP Server   (infrastructure/mcp)│
   (Claude, IDE)    └───────────────┬──────────────────────────┘
                                    │
   Browser ─────────► FastAPI + SSE │  (interface/api)
                                    ▼
                    ┌──────────────────────────────────────────┐
                    │   ReviewService  (application/services)  │
                    │   budget · cache · idempotency           │
                    └───────────────┬──────────────────────────┘
                                    ▼
                    ┌──────────────────────────────────────────┐
                    │   LangGraph review graph                 │
                    │   (application/agents)                   │
                    │                                          │
                    │   ingest → route → specialists →         │
                    │   synthesise → interrupt() → publish     │
                    └───┬───────────────┬──────────────┬───────┘
                        │               │              │
              ports ────┼───────────────┼──────────────┼──── ports
                        ▼               ▼              ▼
              ┌─────────────┐  ┌──────────────┐  ┌────────────┐
              │ GitHub MCP  │  │  Retriever   │  │ ChatModel  │
              │   client    │  │ hybrid+rerank│  │ groq/ollama│
              └─────────────┘  └──────┬───────┘  └────────────┘
                                      ▼
                              ┌───────────────┐
                              │ Neon Postgres │
                              │ pgvector      │
                              │ + audit table │
                              └───────────────┘
```

The four layers are enforced, not conventional:

- `app/domain` — entities, value objects, `Protocol` ports. Zero infrastructure imports.
  Enforced by an import-linter forbidden contract **and** an AST fitness test.
- `app/application` — the agents (supervisor + specialists), services, use cases.
  Depends on `domain` only. May use LangGraph (see ADR-0002).
- `app/infrastructure` — adapters: MCP client, MCP server, retrieval, LLM, persistence,
  observability. Depends on `domain` only. **Cannot import `application`.**
- `app/interface` — FastAPI routers, schemas, and the composition root, which is the one
  place allowed to know about every layer.

`application` and `infrastructure` are *siblings*: neither may import the other. Wiring
happens exactly once, in `app/interface/container.py`.

## 2. The agent graph

```
                          ┌───────────┐
                          │  ingest   │  fetch PR metadata + diff via GitHub MCP
                          └─────┬─────┘  cap diff size, reject oversize
                                ▼
                          ┌───────────┐
                          │   route   │  supervisor picks specialists + logs WHY
                          └─────┬─────┘
              ┌─────────────────┼─────────────────┐
              ▼                 ▼                 ▼
      ┌──────────────┐  ┌──────────────┐  ┌───────────────┐
      │ correctness  │  │   security   │  │ test-coverage │   (sequential by default —
      └──────┬───────┘  └──────┬───────┘  └───────┬───────┘    12K tokens/min ceiling)
             │                 │                  │
             │  each: retrieve(k) → cite-or-drop  │
             └─────────────────┼──────────────────┘
                               ▼
                        ┌─────────────┐
                        │  synthesise │  dedupe, rank, drop uncited (70B model)
                        └──────┬──────┘
                               ▼
                        ┌─────────────┐
                        │ interrupt() │  ◄── DURABLE. Graph state checkpointed.
                        └──────┬──────┘      Process may die here and resume.
                               ▼
                        ┌─────────────┐
                        │   publish   │  write to GitHub + append audit row
                        └─────────────┘
```

### Why a supervisor rather than one agent with three prompts

This is the question I expect to be asked, so the answer is designed in, not rationalised.

1. **Routing is a measurable decision.** A single agent given three concerns in one prompt
   makes that choice invisibly inside one forward pass. A supervisor emits a routing
   decision as data — `{specialists: [...], reason: "..."}` — which can be logged, replayed,
   and scored against a labelled expected set. *Specialist routing accuracy is a published
   metric; it only exists because the supervisor exists.*
2. **Cost.** Skipping a specialist that the diff does not warrant saves a whole LLM call.
   Against a 100K token/day ceiling that is the difference between 2 and 4 reviews.
3. **Context isolation.** Each specialist gets only the retrieved chunks relevant to its
   own concern. One combined prompt would carry all three retrieval sets, tripling input
   tokens and diluting attention.
4. **Independent failure.** A specialist that returns malformed JSON is dropped and logged;
   the other two still produce a review. One monolithic call fails whole.

**What I rejected:** a single "reviewer" prompt with a three-section output schema. It is
cheaper per review (one call, not four) and genuinely simpler. I rejected it because
routing accuracy then cannot be measured at all, and because a single 8B-model call asked
to do three jobs at once degrades noticeably on the third. Recorded in ADR-0003.

### Supervisor routing logic

Routing is **deterministic heuristics first, LLM second** — and the heuristics are the
part I trust.

```python
# Signals computed from the diff, cheaply and reproducibly:
#   - path globs        (auth/, security/, crypto, middleware → security)
#   - test file ratio   (source files changed with no test files → test-coverage)
#   - added-line count  (large logic changes → correctness)
#   - identifier deltas (new public functions without tests → test-coverage)
```

The LLM is asked to *confirm or extend* the heuristic set, never to replace it — the
heuristic floor is always included. This makes routing debuggable at 1am: the log line is
`chose security specialist: diff touches app/auth/, 2 injection heuristics matched`, not
`chose security specialist`.

`correctness` is always included. A diff that warrants no correctness review is a diff
Quorum should not have been asked about.

### The cite-or-drop invariant

**Every surfaced finding carries a resolvable chunk id.** A specialist that produces a
finding with no citation, or a citation that does not resolve to a chunk actually returned
by the retriever for that query, has its finding **dropped** — not downgraded, dropped.

This is the single most important invariant in the system, and it is enforced in code at
the synthesis boundary rather than requested in a prompt, because prompts are advisory and
code is not. Test: `test_uncited_finding_is_dropped`, plus a property test that a
hallucinated chunk id never survives synthesis.

The cost of this invariant is honest: **Quorum cannot report a defect the repository's
documentation does not speak to.** It will miss real bugs. That is a deliberate trade —
a grounded reviewer that misses things is useful; an ungrounded reviewer that invents
conventions is not.

## 3. Retrieval design

```
query ──┬─► dense (bge-small-en-v1.5, 384d, cosine)  ─► top 30 ─┐
        │                                                       ├─► RRF fuse ─► top 30
        └─► BM25 (code-aware tokenizer, k1=1.2, b=0.75) ─► 30 ──┘        │
                                                                          ▼
                                                        cross-encoder rerank (optional)
                                                                          │
                                                                          ▼
                                                                   top 5 chunks
```

**Why hybrid and not dense alone.** Dense embeddings are strong on paraphrase and weak on
exact symbols. A query containing `RetrievalPort` or `QUORUM_MAX_DIFF_LINES` must match the
chunk that literally contains that token; a 384-dimension bi-encoder routinely does not.
BM25 with a tokenizer that splits `camelCase` and `snake_case` catches exactly this.
Fusion is Reciprocal Rank Fusion — it needs no score normalisation between two scorers
whose scales are unrelated.

**Reranking is on probation.** It costs latency on a 512MB Render instance. Phase 3 ships
the retrieval eval alongside the retriever and reports NDCG@5 and Recall@5 **with and
without** reranking. *If reranking does not earn its latency, it gets cut and that is
written up as a finding, not hidden as a failure.*

### Chunk identity — the thing that cannot be got wrong later

Chunks are **chunk-level**, never file-level. Every chunk id is derived from, and resolves
back to, `(repo, commit_sha, file_path, section_path, start_offset, end_offset)`.

```
chunk_id = sha256("{repo}@{sha}:{path}#{section_path}@{start}-{end}")[:16]
```

The tuple is stored in columns alongside the id, so a citation renders as a real link into
a real file at a real line range. File-level ids would silently invalidate every retrieval
number this project publishes. See `Schema.md` §2 and ADR-0005.

## 4. Human-in-the-loop and audit

The approval gate is a LangGraph **durable** `interrupt()` backed by a Postgres
checkpointer, not an in-memory pause. The distinction matters: a free-tier Render instance
sleeps. A review proposed at 14:00 and approved at 19:00 must resume on a different process.

Audit is a **database table, not a log stream** — append-only, never sampled, never
deleted, queryable. Logs rotate; the answer to "why did it post that?" must not.

```
proposed ──► approved ──► posted
        └──► rejected
        └──► edited ──► approved ──► posted
```

Each transition writes one immutable row: `run_id`, `finding_id`, `actor`, `action`,
`payload_hash`, `created_at`. Nothing reaches GitHub without a preceding `approved` row
for that exact finding, verified in code at the publish boundary and covered by
`test_publish_requires_approval_row`.

## 5. Observability — three mechanisms, deliberately not one

| | Purpose | Reader | Retention |
| --- | --- | --- | --- |
| **Logs** | Debug a specific failure | Me, at 1am | Short, sampled |
| **Traces** | Attribute latency and cost across a run | Me, tuning | Medium |
| **Audit** | Prove what the agent did and who approved it | Anyone asking "why did it post that?" | Long, immutable, never sampled |

Conflating these is the common mistake. If audit is a log stream it gets rotated away
exactly when someone needs it. If traces are logs you cannot aggregate cost. They are three
tables/streams with three retention policies.

- Structured JSON to stdout. No log files — Render captures stdout.
- A `run_id` threaded through every event, so one review reconstructs end to end from logs.
- Logging is a `Protocol` port in `domain`; adapters live in `infrastructure`. Domain code
  cannot import a logging library, and import-linter enforces that.
- **Every graph node is traced by the base class**, not by each node remembering. A node
  that is not traced cannot be added — `test_every_node_is_traced` walks the graph registry.
- **Telemetry never fails a request.** Every emit path swallows its own exceptions.
- **Never log secrets, tokens, or raw diff content at INFO.** A diff is attacker-controlled
  and may contain credentials. Redaction happens before the log call, not inside it.

## 6. Data flow, one review

See `AppFlow.md` for the full walkthrough. In one line: *PR reference → cache probe by
commit SHA → MCP fetch → route → retrieve-and-review per specialist → synthesise →
durable pause → human decision → GitHub write → audit row.*

## 7. Cost control, designed in from Phase 0

| Control | Mechanism |
| --- | --- |
| **Cache by commit SHA** | `(repo, pr_number, head_sha, config_hash)` → stored review. Gallery reviews computed once, served forever. Without this nothing else matters. |
| **Model routing** | Specialists on the small model, synthesis on the 70B. Quality measured at both and the delta reported. |
| **Diff cap** | Reject or truncate beyond `QUORUM_MAX_DIFF_LINES`. |
| **Daily budget** | Global token counter; when exhausted, fall back to cached with an honest message rather than a silent failure. |
| **Sequential specialists** | The 12K tokens/minute ceiling makes parallel dispatch trip the rate limit. Concurrency is a config knob, defaulted off, with the reason recorded. |
