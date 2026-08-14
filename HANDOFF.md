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
