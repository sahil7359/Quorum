# HANDOFF

Appended after **every** phase, not once at the end. Read this first.

---

## ⚠ Read this before anything else

### Commit messages are placeholders

You said you write every commit message yourself. You were also asleep, and every phase has
to end in a commit. I could not satisfy both, so I took the option that loses the least:
**every commit message ends with `[MESSAGE PENDING — see HANDOFF.md]`.** The `CHANGELOG.md`
entry for each phase is the "what changed" summary you asked for; write your messages from
it and rewrite history before pushing:

```bash
git rebase -i --root
```

Nothing has been pushed anywhere. There is no remote.

### There is no remote, so "pull requests" are local branches

No `gh` CLI is installed and no `GITHUB_TOKEN` is set, so I could not open real PRs. Each
phase is a branch merged into `main` with `--no-ff`, which keeps the merge-commit shape a
real PR would produce. When you add a remote you can push the branches and open the PRs
retroactively, or just push `main` and accept the history.

### Environment facts that shaped everything

| Fact | Consequence |
| --- | --- |
| `GROQ_API_KEY` **not set** | Zero live Groq calls this run. No Groq number anywhere is real, because none was produced. |
| `GITHUB_TOKEN` **not set** | The official GitHub MCP server cannot authenticate. The client is tested against a fake MCP server speaking the real protocol over real stdio. Unauthenticated GitHub REST (60 req/hr) is available for fixtures. |
| Ollama **running**, RTX 5070 Ti 16GB | Local inference genuinely works: `llama3.1:8b`, `qwen3-coder:30b`, `gpt-oss:20b`, `nomic-embed-text`. |
| Docker **daemon up** | `pgvector/pgvector:pg16` usable for integration tests. |
| Network **reachable** | pypi and api.github.com both respond. |
| No system Python; `uv` present | Project pinned to `uv`-managed CPython 3.12. |

---

## Planning

### What was built

The eleven planning documents in `docs/`, plus the four seeded record artifacts. No code.

`docs/ImplementationPlan.md` ends with a **Reflection** section: I re-read the whole set
critically before writing any code and recorded eight things I would change, marking which
I acted on. The three that changed the plan materially:

- **R1** — Phase 3 is split internally into *3a: chunking and chunk identity* and
  *3b: retrieval, fusion, rerank, eval*, committed separately, because 3a is the part that
  cannot be corrected later.
- **R2** — the retrieval eval's relevance labels are mine, which is grading my own homework.
  The **rerank delta** is the trustworthy number; the absolute NDCG is not. This must stay
  in the write-up.
- **R8** — `config_hash` needed a precise definition or the cache silently serves stale
  reviews. Defined and implemented in Phase 0.

### Decisions I made that you did not specify

| Decision | Why |
| --- | --- |
| Python 3.12, not 3.13 | Native wheels for the retrieval/ONNX stack are still smoother on 3.12. |
| `uv` over Poetry | A coin-flip. `uv` was already installed; one tool for interpreter, venv and lockfile. |
| Dependencies added **per phase**, not up front | Your rule that every dependency needs an explanation. Phase 0 has exactly three. |
| `fastembed` (ONNX) for embeddings and reranking | Render's free tier is 512MB. `sentence-transformers` drags in ~2GB of torch and will not fit. Same model local and prod, so vectors are comparable. **Flagged: this is a real dependency choice you may want to overrule.** |
| Hand-rolled BM25 rather than `rank-bm25` | ~70 lines, and the whole point of the sparse leg is code-identifier matching, which needs a tokenizer that splits `camelCase`/`snake_case`. Owning the tokenizer is the feature. |
| Hand-rolled httpx LLM adapters rather than `langchain-groq`/`langchain-ollama` | Exact token accounting is needed for the daily budget, and provider swap must be a config change. Two ~80-line adapters beat two framework packages. |
| `application` and `infrastructure` as **independent siblings**, expressed as two `forbidden` contracts | The single-layer-line syntax for independent siblings has moved between import-linter versions. Two contracts read unambiguously. |

### What I was unsure about and guessed at

- **Groq model ids.** `llama-3.1-8b-instant` and `llama-3.3-70b-versatile` are taken from
  your brief. I could not verify them against the live API without a key. If Groq has
  retired either, it is a one-line config change — `QUORUM_GROQ_*_MODEL`.
- **Whether `pathlib`/`os` should be banned in `application`.** I banned them, which is
  stricter than "no I/O". The upside is that prompts must be code constants (guardrail G2).
  If Phase 4 finds this painful, it is a deliberate revisit, not a quiet exception.

### What the next phase starts with

Phase 0 starts with an empty `app/` tree and the documents above.

---

## Phase 0 — Scaffolding

**Branch:** `phase/00-scaffolding` → merged to `main` with `--no-ff`
**Suite:** 25 passed · mypy `--strict` clean on 18 files · 6/6 import-linter contracts kept

### What was built, in plain language

The project skeleton. A `uv` project on Python 3.12 with ruff, mypy in strict mode, pytest
laid out in four buckets (`unit`, `integration`, `security`, `architecture`), pre-commit with
gitleaks, a CI workflow, and `.claude/` gitignored from the first commit.

Two parts are more than chores:

1. **The layer boundaries are machine-checked, twice.** Six import-linter contracts, plus
   AST fitness tests that parse every file in `app/domain` and `app/application` and inspect
   their import statements. Two mechanisms because import-linter is *configuration* and can
   be relaxed in the same commit that violates it; weakening a test looks like weakening a
   test.
2. **The review cache key is defined before there is a cache.** `Settings.config_hash()`
   hashes everything that changes what a review *says* (prompt version, chunker version,
   provider, both model ids, retrieval settings, diff cap) and deliberately excludes
   everything that only changes *how we get there* (API keys, base URLs, log level).

### Gate-failure proofs — 3 of 3 run

Per your rule that a test which cannot fail is worse than no test:

| Break introduced | Observed |
| --- | --- |
| `import structlog` in `app/domain/_gate_proof.py` | `test_domain_imports_only_stdlib_and_itself` **FAILED**; contract `Domain is pure` **BROKEN** |
| `import sqlalchemy` + `from app.infrastructure.config import Settings` in `app/application/_gate_proof.py` | 2 tests **FAILED**; contracts `Application does not import infrastructure` and `Application is framework-light` **BROKEN** (4 kept, 2 broken) |
| Froze `prompt_version` inside `config_hash()` | `test_output_affecting_settings_change_the_hash[QUORUM_PROMPT_VERSION-2]` **FAILED** |

All three restored; suite green afterwards, and I checked no `_gate_proof.py` file survived.

### Decisions I made that you did not specify

| Decision | Why |
| --- | --- |
| `include_external_packages = true` for import-linter | Without it, `httpx` is not a node in the graph and "domain must not import httpx" **passes vacuously**. This is exactly the tautological-gate failure you told me not to repeat, and I nearly shipped it. |
| The import-linter test **asserts** rather than skips when `lint-imports` is missing | A silently skipped architecture check is the same as no architecture check. |
| Banned `os`, `pathlib`, `shutil`, `tempfile`, `subprocess` in `application` | Stricter than "no I/O". Forces prompts to be code constants, which *is* guardrail G2 enforced structurally. |
| mypy override block present but **empty** | Packages get named into it individually as they arrive. A global `ignore_missing_imports = true` hides real errors in my own code. |
| Ruff formats Python code blocks inside Markdown | It reformatted one snippet in `docs/AppFlow.md`. I let it — docs and code staying consistent is a small win. Turn it off by excluding `*.md` if it annoys you. |

### What I was unsure about and guessed at

- **`specialist_concurrency` default of 1.** Your brief says the 12K tokens/minute ceiling
  prevents parallel specialists. That is a *Groq free tier* fact, not a design fact, and
  local Ollama has no such limit — so I made it a setting (1–3) defaulted to 1 rather than a
  hardcoded loop. Eval runs would otherwise be needlessly slow.
- **Test-layout choice.** I created `tests/{unit,integration,security,architecture}` to match
  your repo layout. `tests/support/` for shared helpers is mine.

### What the next phase starts with

Phase 1 (Domain core) starts with:

- Empty `app/domain/` apart from `__init__.py` — no entities or ports exist yet.
- `tests/architecture/test_domain_is_pure.py` already watching that package, so the first
  domain module is born under the purity constraint rather than retrofitted into it.
- `Settings.config_hash()` in place, so `Chunk` and `Review` identity work can assume a
  stable cache key exists.
- `docs/Schema.md` §2.1 already specifies the chunk-id derivation and its five invariants.
  Phase 1 should define the `ChunkId` value object to match it exactly; Phase 3 implements
  the chunker that produces them.

---

## Phase 1 — Domain core

**Branch:** `phase/01-domain-core` → merged to `main` with `--no-ff`
**Suite:** 133 passed · mypy `--strict` clean on 27 files · 6/6 contracts kept

### What was built, in plain language

The vocabulary of the system, with no dependencies beyond the standard library:
`values.py` (identifiers, enums, usage accounting), `entities.py` (pull request, diff,
chunk, finding, review, approval, audit), `grounding.py` (cite-or-drop), `ports.py` (11
Protocol ports), `errors.py`.

The two things that matter:

1. **Cite-or-drop is a type transition.** `CandidateFinding` is untrusted model output with
   `chunk_id: str | None`. `Finding` has `citation: Citation` — not optional, not ever. The
   only route between them is `ground_candidates()`, which drops what it cannot ground. The
   invariant survives a wrong caller because a `Finding` with no citation is a `TypeError`.
2. **Chunk identity is chunk-level.** `sha256("{repo}@{sha}:{path}#{section}@{start}-{end}")[:16]`.
   The byte offsets are the whole point — without them every citation degrades to "somewhere
   in this document".

### Decisions I made that you did not specify

| Decision | Why |
| --- | --- |
| Two finding types rather than one with an optional citation | Makes cite-or-drop unforgeable rather than remembered. It also pays off in Phases 7 and 8, where findings reach consumers by two more paths that an assertion in synthesis would not cover. |
| `visible` is **per specialist**, not a global corpus check | A model can cite a *real* chunk it was never shown. My first version only checked existence, which misses this entirely. This is the subtlest guard in the system. |
| Drops are retained and counted (`GroundingResult.drop_rate`) | "How often does the model try to cite something it should not" is a Phase 6 number, and it is invisible if you filter silently. |
| `CodeHostPort` write methods take `approval: Approval` as a **required argument** | An unauthorised write becomes inexpressible rather than merely forbidden. |
| `Approval.authorises(finding)` lives on the entity | The publish guard asks the approval rather than trusting the graph. Checks both identity and `payload_hash`, so edited text loses its approval. |
| `RoutingDecision` refuses an empty reason, and refuses to omit `correctness` | Routing accuracy is a published metric; a decision with no rationale is not debuggable. |
| `Severity.rank` rather than natural ordering | `StrEnum` sorts `high < info < low < medium`. Backwards, and it fails silently. |

### Corrections to earlier documents

- **`docs/Schema.md` was wrong.** It claimed a chunk id "resolves back to" its locator. A
  hash is not reversible. Reworded to *verifiable against*; test renamed
  `test_chunk_id_verifies_against_its_locator`. Worth knowing because you would have been
  asked to demonstrate the round trip.
- `test_chunk_never_spans_files` is deferred to Phase 3 and marked as such in `Schema.md`.
  At this layer it is structurally impossible (a locator names exactly one `file_path`), so
  the meaningful assertion is against the real chunker.

### What I was unsure about and guessed at

- **`deduplicate()` keys on `(chunk_id, file_path, line_start)`.** Two specialists flagging
  the same issue at the same place with *different* citations will not collapse. I judged
  duplicate-but-differently-grounded findings to be genuinely different information. If
  Phase 6 shows this producing noise, the key is the thing to change.
- **`is_test_file` is a heuristic biased towards over-matching.** A false positive costs one
  extra specialist call; a false negative costs a missed review. I shipped a real bug here
  first (enumerated `.test.ts`, missed `.test.tsx`), caught by its own test — it now matches
  `".test." in stem` instead of enumerating extensions.
- **`Finding.confidence` is model-reported and I do not trust it.** It is used only as a
  ranking tiebreaker after severity, never as a threshold. If Phase 6 shows it is noise, drop
  it from `rank_key` rather than trying to calibrate it.

### Gate-failure proofs — 3 of 3 run

| Break introduced | Observed |
| --- | --- |
| `ChunkLocator.canonical()` rewritten to a file-level form | **4 tests failed**, incl. `test_offsets_disambiguate_chunk_ids` |
| Per-specialist visibility check replaced with `if False:` | **2 tests failed**, incl. `test_real_chunk_not_shown_to_this_specialist_is_dropped` |
| `ClockPort.now()` given a real implementation | `test_port_methods_have_no_implementation[ClockPort]` **failed** |

All restored; suite green afterwards.

### What the next phase starts with

Phase 2 (MCP client) starts with:

- `CodeHostPort` already defined, including the approval-as-argument shape. The adapter has
  to satisfy that signature, which is the point.
- `Diff`, `ChangedFile`, `DiffHunk`, `PullRequest`, `RepoRef` ready to be parsed into.
- `CodeHostError` and `ToolNotAllowedError` already defined.
- **No `GITHUB_TOKEN`.** The plan is a fake MCP server built with the real `mcp` SDK speaking
  real stdio, so the protocol is genuinely exercised without a credential. The official
  GitHub MCP server path will be written but **cannot be verified this run** — that must be
  said plainly in the Phase 2 notes rather than implied to work.

---

## Phase 2 — MCP client

**Branch:** `phase/02-mcp-client` → merged to `main` with `--no-ff`
**Suite:** 196 passed (22 over real stdio) · mypy clean on 36 files · 6/6 contracts

### What was built, in plain language

`GitHubMcpClient` reads pull requests through the official GitHub MCP server over stdio and
posts approved findings back. Around it: a tool allowlist, a write-authorisation guard,
connect-time inspection of what the server advertises, and a unified-diff parser producing
domain entities.

The testing approach is the notable part. With no `GITHUB_TOKEN`, I built a **fake GitHub
MCP server using the real MCP SDK** and had the client speak actual MCP to it over a real
subprocess. Four behaviour modes (`normal`, `missing`, `extra`, `erroring`) cover the paths
that matter.

### ⚠ What is NOT verified

**The client has never talked to the real `ghcr.io/github/github-mcp-server`.** Argument
names (`pullNumber`, `owner`, `repo`) and response shapes (`head.sha`, `user.login`) are
taken from its documented schema, not from a live handshake. These are the most likely thing
to be wrong. **First task when a token exists:** run the four read paths against the real
server and correct the fixtures.

The MCP *protocol* layer is genuinely verified — handshake, discovery, structured results,
error propagation, teardown. The GitHub *payload* layer is fixtured.

### A real bug, found because a gate proof failed to fail

The diff parser excluded lines starting with `+++` as "file headers". I removed that
exclusion expecting a test to fail. **The suite stayed green**, which exposed two problems:

1. **Dead code.** The parser only counts inside a hunk; `+++ b/path` headers appear before
   the first `@@` and can never reach the counter. The test named for this behaviour was
   passing for an unrelated reason.
2. **Actively wrong.** An added line whose *content* starts with `++` arrives as `+++...`
   and was being silently uncounted. Not hypothetical — the retrieval corpus is
   documentation, and a `CONTRIBUTING.md` explaining patches contains exactly these lines.

Guard removed (not fixed), two tests added that exercise the real case, and the proof
re-run: reinstating the wrong guard now turns 2 tests red.

**This is the single most valuable thing the "prove it fails" rule has done so far.** I had
a rationale comment, a test with a matching name, and a passing suite — three signals
agreeing, all wrong.

### Decisions I made that you did not specify

| Decision | Why |
| --- | --- |
| Guards in the **client**, not the graph | A graph-node check protects one call path. Phases 7 and 8 add two more callers. See ADR-0003. |
| **Allowed** and **write** are separate predicates | Being on the allowlist must not imply being writable. |
| Missing required *read* tool → refuse to connect | A review that silently skipped the diff would produce zero findings and look like a clean review. |
| Unexpected advertised tools → log at INFO, do not fail | The real server exposes dozens of tools; failing would be absurd, ignoring would hide a server gaining `delete_repository`. |
| Write surface asserted as a test | `test_write_surface_is_exactly_two_tools` — a change to the blast radius shows up in review. |
| One subprocess per client lifetime, not a shared session | A long-lived stdio subprocess needs health checks, restart-on-crash and concurrency control. Quorum does 2–4 live reviews a day; that is solving a performance problem I do not have. |
| Each test opens its own `async with` | An async-generator fixture finalises in a different task, and the MCP session's anyio task group refuses a foreign-task exit. |

### Environment note that may bite later

**`mcp` resolved to v2.0.0, not v1.x.** `FastMCP` does not exist in this version — the class
is `mcp.server.mcpserver.MCPServer` — and result fields are snake_case (`server_info`,
`is_error`, `structured_content`). Most MCP tutorials and the brief's mental model assume
v1. Phase 7 (publishing Quorum's own MCP server) is built on `MCPServer` accordingly.

### What I was unsure about and guessed at

- **`get_file_contents` return shape.** The real server may return base64-encoded content in
  a JSON envelope rather than plain text. The client handles both a bare string and a dict
  with a `content` key, but **does not base64-decode**. If the real server encodes, that is
  a one-line fix and a test.
- **`list_changed_files` is not on `CodeHostPort`.** It is a public method on the client used
  for routing signals. I left it off the port because no application code needs it yet; if
  Phase 4 routing uses it, it should be promoted to the port.
- **Error results vs exceptions.** I treat `is_error` results as `CodeHostError`. Whether the
  real server signals rate limiting that way or by transport failure is unknown, so retry
  and backoff are **not implemented** — I did not want to write a retry policy against
  guessed failure semantics. That is a genuine gap for the live path.

### What the next phase starts with

Phase 3 (Document RAG) — **the highest-risk phase** — starts with:

- `Chunk`, `ChunkId`, `ChunkLocator` already defined and tested, with the id scheme frozen
  and four identity invariants green. **The chunker must produce locators matching
  `ChunkLocator.canonical()` exactly.**
- `ChunkStorePort`, `EmbedderPort`, `RetrieverPort`, `RerankerPort` defined but with no
  adapters.
- `test_chunk_never_spans_files` still owed against the real chunker (deferred here from
  Phase 1; a partial version now exists in `test_diff_parser.py` for hunks).
- Per R1 of the ImplementationPlan reflection, Phase 3 splits into **3a chunking and chunk
  identity** and **3b retrieval, fusion, rerank, eval**, committed separately.
- `fastembed` is not yet installed. Docker is available for `pgvector/pgvector:pg16`.

---

## Phase 3 — Document RAG ⚠ **read the chunk-identity note**

**Branch:** `phase/03-document-rag` → merged to `main` with `--no-ff`
**Suite:** 257 passed · mypy clean on 49 files · 6/6 contracts · retrieval gate PASS

### ⚠ Chunk identity — you asked me to flag this prominently

**Chunk ids are chunk-level and I am confident in them.** The scheme is:

```
canonical = "{repo}@{sha}:{file_path}#{section_path}@{start_offset}-{end_offset}"
chunk_id  = sha256(canonical)[:16]
```

Byte offsets participate, so two chunks from the same file *and the same section* get
different ids. Verified by `test_ids_are_chunk_level_not_file_level`, which builds a long
single-section document and asserts distinct ids, and proven by rewriting `canonical()` to a
file-level form (2 tests red immediately).

**I was not unsure anywhere here**, with one exception worth knowing:

- Offsets are byte offsets into the **UTF-8 encoding after newline normalisation to `\n`**.
  Without normalising, a CRLF checkout on Windows produces different ids for identical
  content. `test_crlf_and_lf_produce_identical_ids` covers it. If you ever ingest on a
  machine that normalises differently, that test is the canary.
- `ChunkLocator.canonical()` is **frozen format**. Changing it re-keys the entire corpus, so
  it moves only with `Settings.chunker_version` and a full re-ingest.

### What was built, in plain language

Documents are split on heading boundaries into overlapping windows, each carrying a full
locator. Retrieval runs dense (fastembed bge-small, 384d) and BM25 (hand-rolled, code-aware
tokenizer) in parallel, fuses by reciprocal rank fusion, and optionally reranks. A retrieval
eval scores four configurations against 20 golden queries and a committed baseline, with a
regression gate.

### The measured result: reranking loses

| config | NDCG@5 | Recall@5 | Success@5 | ms/query |
| --- | --- | --- | --- | --- |
| dense | 0.5768 | 0.5867 | 0.9000 | 8.60 |
| bm25 | 0.5507 | 0.6408 | 0.8500 | 0.47 |
| **hybrid** | **0.5811** | **0.6283** | **0.9500** | **9.10** |
| hybrid+rerank | 0.5018 | 0.5575 | 0.8500 | 812.10 |

Rerank delta **−0.0793 NDCG@5**, **−0.0708 Recall@5**, 89× latency. Disabled by default
(ADR-0004), kept behind a flag. I verified the reranker is not integrated backwards before
accepting this.

### ⚠ A bug you should know about because it nearly shipped

The eval corpus was the live `docs/` tree. The ADR recording the reranking result is itself a
document in `docs/`, so **writing it changed the numbers it recorded** — hybrid NDCG@5 went
0.5943 → 0.5825 → 0.5811 across three runs with zero code change.

Found because the gate failed immediately after I restored from a deliberate break, when it
should have passed. Fixes: every report carries a `corpus_sha` and the gate raises
`CorpusMismatchError` rather than reporting a false regression; and the corpus is now a **frozen
snapshot** in `eval/corpus/`, refreshed only by `--snapshot`.

**Consequence for you:** when you edit `docs/`, the eval does *not* change. To pick up doc
changes deliberately:

```bash
uv run python -m eval.retrieval.runner --snapshot --write-baseline
```

### Decisions I made that you did not specify

| Decision | Why |
| --- | --- |
| **pgvector deferred**, `InMemoryChunkStore` only | Exhaustive cosine over ~1e4 chunks is single-digit ms. An HNSW index trades recall for speed I do not need, and that recall loss would contaminate the numbers this phase exists to produce. pgvector is a deployment concern. **This is a deferral, not an omission** — `ChunkStorePort` exists and a pgvector adapter is a drop-in. |
| Corpus frozen in `eval/corpus/` | Breaks the self-reference loop above. Costs ~200KB of duplicated markdown. |
| Golden-set labels are `(file_path, section_substring)`, not chunk ids | Re-chunking (e.g. changing `target_tokens`) would otherwise invalidate 20 hand-written labels. |
| `Success@5` added as a third metric | Closest to what Quorum actually needs — a specialist needs *one* apt chunk, not a good ordering. NDCG punishes the system for something it does not do. |
| BM25 IDF clamped at zero | The smoothed form goes negative for terms in >half the corpus, *penalising* a document for containing the query term. On a small domain corpus, "chunk" is such a term. |
| Gate split into pure logic + slow CLI | So the comparison logic can be broken on purpose in a millisecond unit test. |
| `hybrid+rerank` not gated | It is disabled by default; failing CI over a config we do not ship is noise. |
| Blank lines trimmed from window edges | Otherwise a citation's line link points at the empty line above the text. |

### What I was unsure about and guessed at

- **`target_tokens=320`, `overlap_tokens=48`** are unmeasured. I did not sweep chunk size,
  and it plausibly matters more than reranking did. **This is the most obvious cheap
  experiment left in retrieval** and the eval harness already supports it.
- **`estimate_tokens` is chars÷4**, not a real tokenizer. It only drives packing; budget
  accounting uses provider-reported counts. Named `estimate` for that reason.
- **20 queries is a small sample.** A ±0.05 swing on NDCG@5 would not surprise me. The gate
  tolerance is 0.02, which may prove too tight; if CI flaps, widen it deliberately rather
  than re-baselining to silence it.
- **Multi-target queries may be depressing NDCG structurally.** Where I labelled two
  documents relevant, retrieval concentrates on the better lexical match. I have not
  separated "retrieval failure" from "labelling artefact" and I should not claim to have.
- **`HybridRetriever` takes `store`/`reranker` as `object`** with `# type: ignore` at the call
  sites, rather than the Protocol types. Structural typing plus the async Protocol methods
  fought me and I chose to ship rather than spend the phase on it. **This is the ugliest code
  in the repo and it should be cleaned up** — the ports exist, the annotations just are not
  wired through.

### What the next phase starts with

Phase 4 (Specialists + supervisor) starts with:

- `HybridRetriever.retrieve()` returning `ScoredChunk`s, and `visible_ids()` in
  `hybrid.py` producing exactly the `Mapping[SpecialistKind, Sequence[ChunkId]]` that
  `ground_candidates()` needs for its per-specialist visibility check. **That wiring is the
  point where cite-or-drop becomes real** — the two halves were built three phases apart and
  have not yet been connected.
- `ground_candidates()` and `deduplicate()` already written and tested (Phase 1).
- **No `ChatModelPort` adapter exists yet.** Phase 4 needs `OllamaChatModel` (local, works
  now) and `GroqChatModel` (no key — write it, cannot verify it).
- LangGraph is **not yet a dependency**. It arrives in Phase 4 with ADR-0002 already written.
- `RecordingLogger` in `tests/support/fakes.py` is available for node tests.

---

# Run summary — stopped after Phase 3

## Why I stopped here, and not at Phase 7

You asked for Phases 0–7. I completed **planning + Phases 0, 1, 2, 3** and stopped.

The instruction I followed: *"If context is filling and later phases would be rushed, stop at
the last phase you can do properly and say so. Phases 5–7 done well beats 5–7 done thinly."*

Phase 3 came in substantially larger than planned — it produced a real measured result that
overturned a design decision, and a self-referential-eval bug that took real work to
diagnose and fix properly. By the end of it, what remained of my context would not have
covered Phase 4 (LangGraph graph, three specialists, supervisor routing with heuristics,
AST-scoped context, the cite-or-drop wiring, a token-reduction measurement, plus tests, gate
proofs and four record artifacts) at the standard of the first four phases.

The failure mode I was avoiding is specific: a half-built agent graph with untested nodes and
a `learn/04` written from intention rather than from code. That would have been worse than
not starting, because you would have had to work out which parts were real.

**Everything committed is green:** 257 tests, `mypy --strict` clean on 49 files, 6/6
import-linter contracts, retrieval gate PASS against a committed baseline. No half-finished
work is on `main`.

## What exists now

| Phase | Status | Evidence |
| --- | --- | --- |
| Planning | ✅ | 11 documents + reflection with 8 recorded changes |
| 0 — Scaffolding | ✅ | 25 tests, boundaries enforced two ways, 3/3 gate proofs |
| 1 — Domain core | ✅ | 133 tests, cite-or-drop as a type transition, 3/3 gate proofs |
| 2 — MCP client | ✅ | 196 tests (22 over real stdio), 3/3 gate proofs, 1 real bug found |
| 3 — Document RAG | ✅ | 257 tests, first real numbers, 3/3 gate proofs, 1 real bug found |
| 4–7 | ⬜ **not started** | — |

**Records:** `learn/00`–`learn/03`, ADRs 0001–0004, `CHANGELOG.md`, `docs/INTERVIEW_BRIEF.md`
and `docs/Tracker.md` all current. Four phases, four learn notes, nothing batched at the end.

## The three riskiest assumptions now baked in

### 1. The GitHub MCP tool contract is fixtured, not verified

**What is assumed:** that the official `ghcr.io/github/github-mcp-server` accepts arguments
named `owner`, `repo`, `pullNumber`, `path`, `ref`, and returns payloads shaped like
`{"head": {"sha": ...}, "user": {"login": ...}}`. All of that came from documentation. **The
client has never spoken to the real server**, because there is no `GITHUB_TOKEN` here.

**Why it is risky:** it is invisible. Every test passes, because my fake server implements
the shapes I assumed. If the real server disagrees, every read path fails on first contact —
and it will fail at the demo, not in CI.

**Cost to reverse:** low, and bounded. Set a token, run the four read paths against the real
server, correct `github_client.py` and `tests/support/fake_github_mcp_server.py` in tandem.
An hour, probably less. The protocol layer — handshake, discovery, structured results, error
propagation — *is* genuinely verified and would not change. **Do this before anything in
Phase 4 depends on the diff shape.**

### 2. Retrieval quality is judged by labels I wrote myself

**What is assumed:** that 20 queries I wrote, against a corpus of my own documentation, with
relevance judgements I made, say something useful about retrieval quality.

**Why it is risky:** the numbers now in `README`-adjacent documents (NDCG@5 0.5811,
Success@5 0.95) look like benchmark figures and are not. More sharply — **ADR-0004 cut a
component on this evidence.** If the labels are biased toward lexical matching (plausible; I
wrote queries while looking at the documents), that would systematically disadvantage a
cross-encoder, which is precisely the thing I disabled.

**Cost to reverse:** moderate. The delta is more robust than the absolute score, and the
reranker is behind a flag rather than deleted, so re-enabling is one config change. But
*re-deciding* honestly needs a corpus and labels I did not author — realistically the Phase 12
gallery repositories. Budget half a session. Until then the honest framing, which is in
`learn/03` and the interview brief, is "on this corpus, with these models, reranking lost" —
never "reranking is not worth it".

### 3. `InMemoryChunkStore` is the only store, and the production one is unwritten

**What is assumed:** that a pgvector adapter will drop cleanly behind `ChunkStorePort` when
persistence lands.

**Why it is risky:** the in-memory store does an exhaustive exact cosine scan. pgvector with
an HNSW index does *approximate* search, so it will return slightly different neighbours —
which means **the committed retrieval baseline may not survive the switch**, and the
regression gate would fire on a change that is a deliberate deployment decision rather than a
regression. There is also a silent-divergence hazard: ingest-time and query-time vectors must
come from the identical model, and nothing currently asserts that.

**Cost to reverse:** low if done deliberately, annoying if discovered late. Write the pgvector
adapter (Docker `pgvector/pgvector:pg16` is available and the daemon is up), run the eval
against it, and **record a second baseline keyed by store type** rather than overwriting the
in-memory one. A test asserting both stores return the same top-k for a fixed query set is
the thing that would catch divergence. Half a session.

## Two bugs worth remembering

Both were found by the rule that a gate must be proven able to fail. Neither would have been
found by writing more tests.

1. **A dead-and-wrong guard in the diff parser** (Phase 2). I removed a `+++` exclusion
   expecting a test to go red; the suite stayed green. The guard was unreachable *and* it
   silently undercounted added lines whose content begins with `++` — which happens in
   documentation about patches, which is exactly our corpus.
2. **A self-referential eval** (Phase 3). The corpus was the live `docs/` tree, so writing
   the ADR that recorded a result changed that result. Three runs, three different numbers,
   no code change.

The pattern in both: *a passing test, a matching name, and a plausible rationale comment can
all agree and all be wrong.* Watching a test go red for the specific line you think it covers
is the only thing that establishes it covers that line.

## What to do next

1. **Read `learn/03`** — it is the best of the four notes and covers the most interview-ready
   material.
2. **Rewrite the commit messages.** Every one ends `[MESSAGE PENDING — see HANDOFF.md]`. The
   per-phase `CHANGELOG.md` entries are the "what changed" summaries. `git rebase -i --root`.
3. **Get a `GITHUB_TOKEN`** and close risk #1 before Phase 4 builds on the diff shape.
4. **Then say "begin Phase 4"** — the Phase 3 handoff section above lists exactly what Phase 4
   starts with, including the one piece of ugly code (`HybridRetriever` taking `object`
   parameters with `# type: ignore` at the call sites) that should be cleaned up first.

## Cheap wins left on the table

- **Chunk-size sweep.** `target_tokens=320` / `overlap=48` are unmeasured, and plausibly
  matter more than reranking did. The eval harness already supports the comparison — this is
  the highest value-per-minute experiment available.
- **`HybridRetriever` type annotations.** The ports exist; they just are not wired through.
  Twenty minutes, removes the ugliest code in the repo.

---

## Phase 4 — Specialists + supervisor

**Branch:** `phase/04-multi-agent` → merged to `main` with `--no-ff`
**Suite:** 404 passed · mypy clean on 70 files · 6/6 contracts · retrieval gate PASS

### What was built, in plain language

The agent. A LangGraph graph `ingest → route → specialists → synthesise`; two LLM adapters
behind one Protocol; prompt construction with fencing; AST context scoping; the supervisor's
routing; and the wiring that finally makes cite-or-drop run.

### The design decision to remember

**Routing cannot be talked out of a security review.** Deterministic heuristics compute a
floor; the model may only *add* to it. The diff is attacker-controlled and the router reads
it, so a pure-LLM router is the softest target in the system. ADR-0005 has the full argument
including what over-routing costs.

### Numbers measured

| Number | Value |
| --- | --- |
| Context reduction (token-weighted) | **34.86%** (292,259 → 190,373 tokens) |
| Median per-commit reduction | 38.65% |
| Python-only reduction | 33.76% |
| AST regions / window fallbacks | 391 / 112 |

Measured over 8 real commits from this repo's own history. Committed at
`eval/baselines/scoping.json`, reproduce with `uv run python -m eval.scoping.runner`.

**I expected Python-only to be much higher** (markdown always takes the window fallback, so I
assumed it was dragging the average down). It came back *lower*. Hypothesis wrong; both
numbers reported.

### ⚠ Still not measured

**No LLM has produced a review in this repository.** Every graph test runs against
`FakeChatModel`. Finding precision, finding recall and specialist routing accuracy remain
`TODO: not yet measured`. Phase 6 is where those get evidence.

The Ollama adapter is written and typed but **has never been run against the live Ollama
server** — the graph tests do not need it. That is a 10-minute check worth doing early:

```bash
uv run python -c "import asyncio,httpx; from app.infrastructure.llm.ollama import OllamaChatModel; from app.domain.ports import ChatMessage; from tests.support.fakes import RecordingLogger; m=OllamaChatModel(base_url='http://localhost:11434', model='llama3.1:8b', logger=RecordingLogger()); print(asyncio.run(m.complete([ChatMessage('user','Reply with JSON {\"ok\":true}')], node='smoke')).content)"
```

The Groq adapter is written against the documented OpenAI-compatible schema and **cannot be
verified** without a key. Its `usage` field names and `response_format` handling are the most
likely things to be wrong.

### Decisions I made that you did not specify

| Decision | Why |
| --- | --- |
| Hand-rolled httpx adapters, not `langchain-groq`/`langchain-ollama` | Exact provider-reported token counts are needed for the daily budget; an estimate that drifts makes the cap meaningless. |
| Hand-rolled retry, not `tenacity` | Only idempotent failures are retried. A 400 means the request is malformed and retrying burns 3× quota against a 100K/day cap. |
| `estimate_tokens` moved from the chunker to `app/domain/text.py` | The application layer needs it and cannot import infrastructure. |
| Graph **ends at `synthesise`**, no stub publish node | A stub that posts nothing is indistinguishable in a test from a guard that refuses to post. That is the one distinction this project cannot blur. Phase 5 inserts `interrupt()` and `publish`. |
| `log_events.py` constants + `docs/Logging.md` + 3 enforcement tests | You asked for every log documented with a brief why. Making it a test means it cannot go stale. |
| `finding.dropped` at INFO, `specialist.failed` at WARN | Dropping an uncited finding is correct behaviour, not degradation. One specialist failing is handled; escalating it to ERROR trains you to ignore ERROR. |
| `MAX_FINDINGS_PER_SPECIALIST = 8` | A specialist returning 40 findings is pattern-matching noise, and it blows the synthesis prompt budget. Blunt, and I would revisit it with eval evidence. |
| Retrieval query = specialist concern + changed symbols, **not the whole diff** | Embedding a whole diff retrieves whatever the diff is *about* — the security specialist gets feature chunks and nothing about security. |

### Bugs found this phase

1. **README-only PRs summoned the test-coverage reviewer.** The heuristic keyed on "any
   non-test file". `ChangedFile.is_code_file` now excludes documentation. Found by a test
   whose expectation I nearly "fixed" instead.
2. **Nine bare log-event strings** in the Phase 2/3 modules, caught the moment
   `test_no_bare_event_strings_at_call_sites` was written.
3. **A gate proof that failed to fail.** Breaking `visible[specialist] = list(corpus)` stayed
   green, because the corpus is built incrementally and did not yet contain the later
   specialist's chunk. The real failure shape is in synthesis. Second time this has happened;
   the lesson is that breaking something *plausible* is not the same as breaking the specific
   thing the test claims to guard.

### What I was unsure about and guessed at

- **`FALLBACK_CONTEXT_LINES = 12`** for non-Python files is a guess. Unmeasured.
- **The security regex list is Python-biased** and will need extending for other ecosystems.
- **`_enclosing_spans` keeps the smallest span covering each target**, so a change to a method
  ships the method, not the class. If a specialist needs the class docstring for context, this
  is the thing to revisit.
- **Synthesis does not currently call an LLM.** It deduplicates and ranks in code. The
  `SYNTHESIS_SYSTEM_PROMPT` exists and is unused. I judged that a 70B call to reorder a list
  that code can already order was not worth the tokens — but the plan said synthesis runs on
  the big model, so **this is a deliberate deviation you may want to reverse.** The prompt is
  ready if you do.
- **`Diff.has_source_changes` is now unused** by routing (replaced by `has_code_changes`) but
  is still on the entity. Harmless; flagging so it does not look accidental.

### What the next phase starts with

Phase 5 (HITL + audit) starts with:

- A graph ending at `synthesise` with `state["findings"]` populated and ranked.
- `Approval.authorises(finding)` and `Finding.payload_hash` already written and tested
  (Phase 1), and the `GitHubMcpClient` write guard already refusing unauthorised posts
  (Phase 2). **Phase 5 is mostly wiring plus persistence**, not new invariants.
- `langgraph` installed; `interrupt()` and `Command` imported and verified available.
- **No checkpointer, no database, no persistence of any kind yet.** Durable `interrupt()`
  needs one — `langgraph-checkpoint-sqlite` locally, `-postgres` for prod. Docker is up and
  `pgvector/pgvector:pg16` is available.
- `AuditPort`, `ReviewCachePort` and `BudgetPort` are defined in `app/domain/ports.py` with
  **no adapters at all**.

---

## Addendum — the first live review (run after Phase 4 was committed)

`uv run python -m eval.smoke.live_review`, against local Ollama (`llama3.1:8b`) and the real
hybrid retriever over the 164-chunk frozen corpus. One hand-written diff that concatenates a
system prompt with untrusted diff content — a change the corpus explicitly forbids.

**It worked end to end.**

```
routing     : ['correctness', 'security', 'test_coverage']
  heuristics: correctness (always); test_coverage (source changed, no tests);
              1 new public symbol: build_prompt
  model added: security — "new public symbol 'build_prompt' suggests the change is
               significant enough to warrant security review"
cost        : 3 model calls, 6,967 tokens, 16.1s wall clock
findings    : 2 surfaced, 0 dropped
```

The security finding is **genuinely right and aptly grounded**:

> `[high] security: Diff content is untrusted and should be fenced`
> cites `33e495d306e9c1ad` → `docs/Guardrails.md — Guardrails > 2. Controls`

That is the system doing exactly what it exists to do: it found a real violation of a rule
written in the repository's own documentation, and cited the rule.

The correctness finding is **misgrounded**, and I am glad it happened on run one:

> `[high] correctness: Logic error: system prompt is not constant`
> cites `b0367541579451eb` → `docs/AppFlow.md — Application Flow > 5. route node`

The *claim* is reasonable. The *citation does not support it* — the `route` node section of
AppFlow has nothing to say about prompt constancy. Cite-or-drop passed it because the chunk is
real and was shown to that specialist, which is all cite-or-drop checks.

**This is the limitation I wrote down in Phase 1, demonstrated in practice on the first real
run: a citation proves grounding, not aptness.** I have no test for aptness. What I have is
retrieval quality bounding how often the wrong chunk comes back, and the citation rendered in
the output so a human can check it in one click — which is precisely what let me catch this in
about four seconds.

A second, smaller observation: the model wrote the reference marker into its prose
(*"according to [2] chunk_id: b03675415794"*). Harmless, and a prompt-formatting fix, but the
kind of thing you only see by running it.

**What this is not:** a metric. One hand-written diff, one model, `n=1`. It says the stack
holds together; it says nothing about review quality. That evidence comes in Phase 6, against
merged pull requests carrying real human review comments.

---

## Phase 5 — HITL + audit

**Branch:** `phase/05-hitl-audit` → merged to `main` with `--no-ff`
**Suite:** 437 passed · mypy clean on 76 files · 6/6 contracts · retrieval gate PASS

### What was built, in plain language

The approval gate. The graph now runs `ingest → route → specialists → synthesise → approval →
publish`, where `approval` blocks on a **durable** `interrupt()` and `publish` is the only
write path. Behind it, an append-only audit table.

The load-bearing test builds **two separate savers and two separate graph objects over the
same SQLite file** — the second knows nothing except what was checkpointed. That is the
free-tier instance sleeping and waking up, and it is why the checkpointer is durable rather
than in-memory.

### Decisions I made that you did not specify

| Decision | Why |
| --- | --- |
| **SQLite for the audit table, not Postgres** | Durability and append-only triggers, with no service dependency in local dev or CI. **This is a deferral, not a decision against Postgres** — see the risk below. |
| `approval` and `publish` are **optional** graph stages | A caller that only wants findings (MCP server, eval harness) gets a graph with *no write path at all*, rather than a write path it is trusted not to reach. Absence beats discipline. |
| No findings → no interrupt | Stopping to ask a human about an empty list is how an approval gate becomes noise, and a gate people click through is not a gate. |
| No timeout, no auto-approve, no `approve_all` | A timeout that approves is approval with extra steps. |
| A rejection is returned, not `None` | "The human said no" and "nobody has looked" are different states and must not both read as absence. |
| Refusals are themselves audited | The record has to show what was refused, not only what was posted. |
| Graph state keyed by strings, not value objects | Forced by the checkpoint serialiser. Value objects are rebuilt at the domain boundary. |

### ⚠ Risk introduced this phase: SQLite audit ≠ Postgres audit

The append-only guarantee is currently enforced by **SQLite triggers**. Postgres uses a
*different mechanism* (`CREATE RULE ... DO INSTEAD NOTHING`, as written in `docs/Schema.md`).

**The Postgres append-only rules have never been executed.** When the Postgres adapter lands,
`test_update_is_refused_by_the_database` and `test_delete_is_refused_by_the_database` must be
re-run against it — they are testing a mechanism, and the mechanism changes. Do not assume
the SQLite green carries over. Docker is up and `pgvector/pgvector:pg16` is available.

### Bugs and surprises

1. **`TypeError: Dict key must a type serializable with OPT_NON_STR_KEYS`** — graph state held
   `dict[ChunkId, Chunk]` and the checkpoint serialiser cannot encode a frozen dataclass as a
   dict key. Durable resumption constrains what state may contain, which I had not considered
   when designing state in Phase 4. Only surfaced because the test crossed a real process
   boundary.
2. **A gate proof failed to fail, for the third time.** Swapping the publish node's audit
   lookup for a state lookup left all six publish tests green — they all call the node with no
   `approvals` in state, so both implementations refused for the same reason. Added
   `test_an_approval_present_only_in_state_does_not_authorise_a_post`, which forges an approval
   in state with an empty audit log. Now the proof turns red.

### What I was unsure about and guessed at

- **`approval_for` returns the latest decision by `audit_id`.** If a human rejects, then
  re-approves after an edit, the approval wins. I believe that is right; if you disagree, the
  ordering is one `ORDER BY` clause.
- **The resume payload shape** (`[{finding_id, action, actor, note}]`) is mine. Phase 8's API
  has to produce it, and nothing yet validates it end to end from HTTP.
- **`ApprovalNode` writes `proposed` rows before the interrupt**, so a crash between the audit
  write and the checkpoint leaves proposed rows for a run that never paused. Harmless (audit
  is append-only and over-recording is safe) but worth knowing when reading the table.
- **No `ReviewCachePort` or `BudgetPort` adapter yet.** Both are still defined-and-unimplemented.
  The cost controls described in `TechSpec.md` §5 are **not enforced anywhere in code**.

### What the next phase starts with

Phase 6 (Trajectory eval) starts with:

- A complete review pipeline that runs end to end against real Ollama
  (`uv run python -m eval.smoke.live_review`) and returns grounded findings.
- `build_review_graph(..., approval=None, publish=None)` gives the eval harness a graph with
  **no write path**, which is what an eval should have.
- `eval/retrieval/` as the pattern to copy: golden set, metrics, runner, committed baseline,
  and a gate split into pure comparison logic plus a slow CLI.
- **The hard part is the golden set.** It needs merged PRs carrying substantive human review
  comments. Unauthenticated GitHub REST allows 60 requests/hour, which is enough to assemble
  and commit fixtures. **If enough labelled PRs cannot be assembled, ship the harness with
  `TODO: not yet run` and say so** — a plausible-looking metric that was never measured is the
  one thing that must not happen.
- Still unmeasured: finding precision, finding recall, specialist routing accuracy, cost per
  review.

---

## Phase 7 — MCP server *(taken ahead of Phase 6, deliberately)*

**Branch:** `phase/07-mcp-server` → merged to `main` with `--no-ff`
**Suite:** 462 passed · mypy clean on 79 files · 6/6 contracts

### Why out of order

Phase 6 needs merged PRs carrying substantive human review comments — assembled from a
rate-limited API — plus long local model runs, and its entire value is *measurement*. Done
without enough budget it yields a harness and a `TODO`, or worse a number nobody measured.

Phase 7 is self-contained, closes a Definition-of-Done item, and uses the write-path-free
graph option built in Phase 5. Ordering is a convenience; getting a number wrong is not.

### What was built

Quorum published as an MCP server: `review_pull_request`, `get_review`, `list_ingested_repos`,
`get_chunk`. Schema in `docs/MCP.md`. Tested with a **real MCP client over real stdio** — the
mirror of Phase 2.

**The surface has no write path, structurally.** Three read-only callables; the graph behind
`review` is built with `approval=None, publish=None`; the module imports neither
`GitHubMcpClient` nor `PublishNode`, asserted by a test that parses its AST.

### Decisions I made that you did not specify

| Decision | Why |
| --- | --- |
| `routing_reason` returned to the client, not just logged | Routing accuracy is a published metric; a caller deserves the rationale too. |
| `dropped` returned to the client | "The model tried to cite something it wasn't shown" is information about reliability. A caller that never sees it cannot tell a quiet review from a suppressed one. |
| `get_chunk` published | Lets a client resolve a cited chunk id without our database. Turns cite-or-drop from a claim into an auditable property. **This is the tool I would highlight.** |
| Every response carries `posted_to_github: false` | A client should not have to read the README to learn we did not touch their repo. |
| stdio only | Streamable HTTP arrives with FastAPI in Phase 8, where there is an ASGI app to mount it on. |
| Server takes callables, not a graph | The transport is testable without a model, retriever or code host. |

### What I was unsure about and guessed at

- **`reviews` is an in-process dict.** `get_review` only finds reviews computed by *this*
  process. Real persistence is the `ReviewCachePort`, still unimplemented.
- **The stdio entrypoint module referenced in `docs/MCP.md`
  (`app.interface.mcp_entrypoint`) does not exist yet** — the composition root lands in
  Phase 8. `tests/support/quorum_mcp_stdio.py` is the runnable example until then. This is
  flagged inline in the doc, but it is the one thing in `docs/MCP.md` a reader could try and
  fail to run.
- **`get_chunk` has no rate limiting.** It reads from our chunk store; on a public deployment
  it is an enumeration surface. Phase 9/10 concern, noted now.

### Gate proofs — 3 of 3

| Break | Result |
| --- | --- |
| A `post_review_comment` tool added to the published surface | 3 tests **red** |
| `posted_to_github` flipped to `true` | 1 test **red** |
| Repo validation replaced with a bare constructor | 1 test **red** |

### What Phase 6 starts with — scoped precisely

Phase 6 (Trajectory eval) is **the only remaining gap in Phases 0–7**. It starts with:

1. **A working pipeline.** `uv run python -m eval.smoke.live_review` runs a complete review
   against local Ollama in ~16s and returns grounded findings.
2. **`build_review_graph(..., approval=None, publish=None)`** gives the harness a graph with no
   write path — what an eval should have.
3. **The pattern to copy is `eval/retrieval/`**: golden set → metrics → runner → committed
   baseline → gate split into pure comparison logic plus a slow CLI. Reuse
   `check_regression`'s shape, including the corpus-fingerprint lesson.
4. **The hard part is the golden set.** Needs merged PRs with substantive human review
   comments. Unauthenticated GitHub REST gives 60 req/hour — enough to assemble and commit
   fixtures, and fixtures are what make the eval reproducible. Suggested shape:
   `eval/trajectory/goldenset/<owner>-<repo>-<pr>.json` holding the diff, the PR metadata, the
   human review comments, and a hand-labelled expected specialist set.
5. **Metrics owed:** finding precision, finding recall, specialist routing accuracy, tool-call
   correctness, steps and cost per review.
6. **`learn/06` must state** that human reviewers miss things, so recall is measured against an
   **imperfect ceiling**.

**If enough labelled PRs cannot be assembled, ship the harness, write `TODO: not yet run`, and
say so plainly.** A plausible-looking metric that was never measured is the single worst thing
that could be left here.

### Cost note for whoever runs it

A full 20-PR run against Ollama is roughly 20 × 3 model calls × ~7K tokens ≈ 400K+ tokens and
perhaps 15–25 minutes of local GPU time. That is fine locally and would **exhaust the Groq free
tier four times over** — which is exactly why the provider abstraction exists. Run it with
`QUORUM_LLM_PROVIDER=ollama`, never Groq.
