# Quorum — Technical Specification

## 1. Stack

| Concern | Choice | Why this and not the alternative |
| --- | --- | --- |
| Language | Python 3.12 | Matches the LangGraph/MCP ecosystem. 3.13+ still has rough edges in native wheels. |
| Packaging | `uv` + `pyproject.toml` | Fast, lockfile-first, single tool for venv + deps. |
| Orchestration | `langgraph` | Locked. Durable `interrupt()` is the reason — no other option gives a resumable human gate for free. |
| LLM access | **hand-rolled httpx adapters** | Rejected `langchain-groq` / `langchain-ollama`. I need exact token accounting for the daily budget, and provider swap must be a config change. Two ~80-line adapters behind one Protocol beat two framework packages I would have to explain. |
| MCP | official `mcp` Python SDK | Both client (consume GitHub MCP) and server (publish Quorum). |
| Validation | `pydantic` v2 + `pydantic-settings` | Structured LLM output parsing and typed config. |
| Embeddings | `fastembed` (`BAAI/bge-small-en-v1.5`, 384d) | ONNX CPU, ~130MB, fits a 512MB Render instance. Rejected `sentence-transformers` (drags in ~2GB of torch — will not fit the free tier). Same model local and prod, so vectors are comparable. |
| Sparse retrieval | **hand-rolled BM25** | ~70 lines. Rejected `rank-bm25` because the whole point of the sparse leg is code-identifier matching, which needs a custom tokenizer splitting `camelCase`/`snake_case`. Owning the tokenizer is the feature. |
| Rerank | `fastembed` TextCrossEncoder | Same dependency family, ONNX, no new stack. On probation — see Phase 3. |
| Persistence | Postgres (Neon) + `pgvector`, via SQLAlchemy 2.0 + `psycopg` | Locked by the $0 constraint. Local integration tests run against Dockerised `pgvector/pgvector:pg16`. |
| Checkpointer | `langgraph-checkpoint-postgres` (prod), `-sqlite` (local) | Required for durable `interrupt()`. |
| Logging | `structlog` | JSON to stdout, one dependency, `contextvars` binding gives `run_id` propagation without threading it manually. |
| API | `fastapi` + `uvicorn` + `sse-starlette` | Phase 8. |
| Frontend | Next.js on Vercel | Phase 12/13. |

**Dependency rule:** every runtime dependency above is one I can explain in an interview,
including what it replaced. Nothing gets added silently.

## 2. Layers and ports

```
app/
  domain/
    entities/        PullRequest, Diff, Finding, Review, Chunk, Citation, Approval, RunId
    values/          ChunkId, Severity, SpecialistKind, ApprovalAction, TokenUsage
    ports/           Protocol definitions only — no implementations
  application/
    agents/          supervisor, specialists/{correctness,security,test_coverage}, graph
    services/        ReviewService, BudgetService, CacheService, ApprovalService
  infrastructure/
    mcp/             github_client.py (consume), quorum_server.py (publish)
    retrieval/       chunker, bm25, dense, fusion, rerank, pgvector_store
    llm/             groq.py, ollama.py, routing.py
    persistence/     models, repositories, migrations
    observability/   logging, tracing, audit
  interface/
    api/             routers, sse
    schemas/         request/response models
    container.py     composition root — the only module that imports every layer
```

### Ports (all `typing.Protocol`, all in `app/domain/ports`)

| Port | Method surface (abridged) | Adapters |
| --- | --- | --- |
| `ChatModelPort` | `complete(messages, *, schema) -> Completion` | `GroqChatModel`, `OllamaChatModel`, `FakeChatModel` |
| `CodeHostPort` | `get_pull_request`, `get_diff`, `get_file`, `post_review_comment` | `GitHubMcpClient`, `FakeCodeHost` |
| `RetrieverPort` | `retrieve(query, k, filters) -> list[ScoredChunk]` | `HybridRetriever`, `FakeRetriever` |
| `EmbedderPort` | `embed(texts) -> list[Vector]` | `FastEmbedEmbedder`, `HashEmbedder` (tests) |
| `ChunkStorePort` | `upsert`, `get_by_id`, `search_dense`, `all_for_repo` | `PgVectorChunkStore`, `InMemoryChunkStore` |
| `AuditPort` | `append(event) -> AuditRow`, `history(run_id)` | `PgAuditLog`, `InMemoryAuditLog` |
| `LoggerPort` | `info/warn/error/debug(event, **fields)` | `StructlogLogger`, `NullLogger` |
| `TracerPort` | `span(name, **attrs)` context manager | `StructlogTracer`, `NullTracer` |
| `ClockPort` | `now() -> datetime` | `SystemClock`, `FrozenClock` |
| `CachePort` | `get(key)`, `put(key, value)` | `PgReviewCache`, `InMemoryCache` |
| `BudgetPort` | `reserve(tokens)`, `record(usage)`, `remaining()` | `PgBudget`, `InMemoryBudget` |

Domain defines these; nothing in `domain` imports an adapter. Every adapter is
constructor-injected at `app/interface/container.py`.

## 3. MCP contracts

### 3.1 Client — consuming the official GitHub MCP server

Transport: **stdio**, launching `ghcr.io/github/github-mcp-server` in Docker with a scoped
PAT passed by environment, never on the command line (argv is world-readable via `ps`).

Tools consumed, and **only** these — the client holds an allowlist and refuses anything
outside it, so a compromised or updated server cannot widen Quorum's reach:

| Tool | Use | Direction |
| --- | --- | --- |
| `get_pull_request` | metadata, head SHA | read |
| `get_pull_request_files` | changed paths + patch | read |
| `get_pull_request_diff` | unified diff | read |
| `get_file_contents` | full file for AST scoping | read |
| `add_pull_request_review_comment` | post an approved finding | **write — gated** |
| `add_issue_comment` | post the review summary | **write — gated** |

The two write tools are reachable **only** from the `publish` node, and only with an
`approved` audit row for the exact finding. Enforced in the client itself
(`test_write_tool_rejected_without_approval_token`), not merely by graph topology, because
graph topology is a convention and a guard is not.

### 3.2 Server — publishing Quorum

Transport: stdio (local clients) and streamable HTTP (remote). Tools published:

| Tool | Input | Output |
| --- | --- | --- |
| `review_pull_request` | `{repo, pr_number, specialists?, live?}` | `{run_id, findings[], citations[], status}` |
| `get_review` | `{run_id}` | cached review or `not_found` |
| `list_ingested_repos` | `{}` | the six gallery repos + ingest commit SHAs |
| `get_chunk` | `{chunk_id}` | chunk text + `(file, section, offset)` locator |

`review_pull_request` **never posts to GitHub.** Publishing is a separate, human-gated
action available only through the web UI. An MCP client calling Quorum gets findings, not
side effects — otherwise the approval gate could be bypassed by calling the tool.
That is the single most important line in this section.

## 4. Model routing

| Node | Groq (prod) | Ollama (local eval) | Reason |
| --- | --- | --- | --- |
| `route` | `llama-3.1-8b-instant` | `llama3.1:8b` | Classification over heuristic signals. Small model is sufficient. |
| specialists | `llama-3.1-8b-instant` | `llama3.1:8b` | 3 calls per review; the small model is the only way to fit the budget. |
| `synthesise` | `llama-3.3-70b-versatile` | `qwen3-coder:30b` | Dedupe + rank + drop needs the stronger model; it runs once. |

Routing table lives in config, not code. Phase 4 measures specialist quality at 8B vs 70B
and reports the delta rather than assuming the split is right.

## 5. Cost controls

| Control | Where | Behaviour |
| --- | --- | --- |
| SHA cache | `CacheService` | Key `sha256(repo, pr, head_sha, config_hash)`. Config hash included so a prompt change invalidates. |
| Diff cap | `ingest` node | `> QUORUM_MAX_DIFF_LINES` → truncate with an explicit banner in the review, or reject. |
| Daily budget | `BudgetService` | Pre-flight `reserve()`; on exhaustion, serve cached and say so. Never silently degrade. |
| Live-review cap | API rate limiter | `QUORUM_LIVE_REVIEWS_PER_DAY`, global not per-IP. |
| Sequential specialists | graph config | Default. 12K tokens/min makes parallel dispatch trip the limit. |

## 6. Typing, testing, enforcement

- `mypy --strict` over `app/`, `eval/`, `tests/`. No `Any` escapes without a `# type: ignore[code]`
  carrying a reason.
- `ruff` for lint + format.
- `import-linter` contracts (in `pyproject.toml`):
  1. **Layers** — `interface` above (`application` | `infrastructure`) above `domain`;
     application and infrastructure are independent siblings.
  2. **Domain is pure** — `app.domain` may not import `httpx`, `sqlalchemy`, `langgraph`,
     `structlog`, `mcp`, `fastembed`, `fastapi`, `psycopg`.
  3. **Application is framework-light** — `app.application` may not import `httpx`,
     `sqlalchemy`, `mcp`, `fastembed`, `fastapi`, `psycopg`. (LangGraph *is* permitted —
     see ADR-0002 for why, and what that costs.)
- Architecture fitness tests in `tests/architecture/` that parse the AST rather than trust
  convention.
- Every gate is proven to fail before it is committed: break the thing it guards, watch it
  go red, restore. Recorded per phase in `HANDOFF.md`.

## 7. Environments

| | Local dev | CI | Production |
| --- | --- | --- | --- |
| LLM | Ollama | `FakeChatModel` | Groq |
| Postgres | Docker `pgvector/pgvector:pg16` on :5433 | GH Actions service container | Neon |
| Embeddings | fastembed (CPU) | fastembed, cached | fastembed (CPU) |
| GitHub | fake MCP server over real stdio | fake MCP server | official GitHub MCP server |
| Eval | full, against Ollama | retrieval eval + trajectory gate | never |
