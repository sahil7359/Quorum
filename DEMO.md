# Quorum — demo, setup & test guide

Everything needed to run Quorum, test it, and present it. Written for two audiences: someone
setting it up from scratch, and me walking an interviewer through it live.

- **Live frontend (dashboard):** _(set once the frontend service is deployed — see [Deploy the frontend](#6-deploy-the-frontend-detailed-runbook))_
- **Live backend:** https://quorum-aka2.onrender.com — the base URL now returns a service
  description; `/docs` is the Swagger UI; `/api/status`, `/api/reviews`, `POST /api/reviews`.
- **Source:** https://github.com/sahil7359/Quorum

---

## 1. What it is, in one breath

Quorum is a supervisor agent that reviews a pull request the way a careful human would: it reads
the diff, decides which specialists (correctness, security, test-coverage) the change warrants,
retrieves the repo's *own* documentation, and lets each specialist propose findings — but a
finding is only shown if it can cite a specific chunk of that documentation. **Ungrounded
findings are dropped, never invented.** Approved findings are posted back to the PR as review
comments by a scoped GitHub App.

The one line to lead with: **"It's a code reviewer that has to cite its sources, and it's built
so that 'the agent cannot do X' is enforced by types and tests, not by hoping."**

---

## 2. The 60-second live demo

> **Which surface to demo.** Two ways to show this, pick per setting:
> - **Local (recommended for the full experience):** run backend + frontend on your laptop
>   (section 4). Memory isn't constrained, so a *first-time* review of any repo works and you
>   get the rich result — the mypy example returns **5 findings, each with a citation**. This is
>   the impressive demo.
> - **Hosted URL (proof it's really deployed):** the free 512MB tier can't index a large repo's
>   docs from scratch (see [limitations](#8-known-limitations-state-them-before-youre-asked)), so
>   on the hosted demo lead with the **already-cached** example (`psf/black #5280`) — it returns
>   instantly and reliably. Use it to prove the deployment is live, then show the depth locally.

1. Open the frontend (a dashboard). Point at the **status bar** first: backend online, the
   model in use, today's token budget — proof it's a live system, not a mockup.
2. Click the **first example** ("already indexed — returns instantly" on the hosted demo;
   **python/mypy #21647** locally for the full 5-finding result).
3. Narrate the streaming steps as they fill in live, with the **process-log console** underneath
   showing the raw events timestamped in real time:
   - **Indexing repository docs** — first review of a PR fetches and chunks the repo's docs
     (this is cached, so it only happens once per commit).
   - **Reading the diff** — "17 files changed, context scoped down 97%" — it AST-scopes the diff
     to the enclosing function/class instead of sending whole files.
   - **Deciding which specialists to run** — heuristics compute a floor; the model may only
     *add* specialists, never remove one (a security check can't be talked out of running).
   - **Running specialists** — each retrieves docs and proposes candidate findings.
   - **Grounding and finalising** — "N findings survived citation checks." Some candidates get
     dropped here for failing to cite — that's the point.
4. Each finding renders with its **severity, specialist, and the exact doc line it cites.**
5. Point at the **Recent reviews** panel on the right — every completed review is there
   (persisted in Postgres), expandable to its findings. Clicking a past one is instant.

> **Warm it up first.** The *first* review of any PR indexes its docs (~30s) and runs the model
> live. Click both examples once before presenting so they're cached and return instantly. On
> the deployed backend there's a **daily live-review cap** (4/day by default) — a cache hit does
> not count against it, so pre-warmed examples demo freely.

### The write path (posting back to GitHub)

The reviewer doesn't just display findings — it posts them to a real PR as a **GitHub App**
(`quorum-reviewer-sahil[bot]`), gated behind human approval. See it on the demo PR:
**https://github.com/sahil7359/test-repo/pull/1** — an inline review comment on the exact line,
rendered with its grounding citation, plus a summary comment. To regenerate it live:

```bash
uv run python -m scripts.seed_test_pr   # opens the demo PR (human identity, via PAT)
uv run python -m scripts.write_demo     # posts an approved review comment (App identity)
```

---

## 3. What to show in the "project" tab (interview)

Lead with the **live frontend demo** (section 2), then pull up these, in order:

| Show | Why it lands |
| --- | --- |
| The **live review streaming** in the frontend | Proves it's real and end-to-end, not a mock. |
| A **finding's citation** ("Grounded in `SECURITY.md` — …") | The whole thesis: grounded, not hallucinated. |
| The **posted comment** on test-repo PR #1 | It closes the loop back to GitHub, as a scoped bot. |
| [`LEARN.md`](LEARN.md) | The strongest artifact — a change log of **real bugs found by running things**, each with the reasoning. Interviewers remember the CRLF-SSE bug and the "green test for the wrong reason" stories. |
| [`docs/AppFlow.md`](docs/AppFlow.md) + the architecture diagram in [`README.md`](README.md) | Ports-and-adapters, the LangGraph pipeline, the log/trace/audit split. |
| [`learn/HLD.md`](learn/HLD.md) / [`learn/LLD.md`](learn/LLD.md) | System-design depth for a design round. |
| The **CI page** on GitHub (all green) | mypy --strict, import-linter boundaries, real Postgres integration tests, container build, retrieval eval gate. |

**Three talking points that separate this from a toy:**

1. **Cite-or-drop is a type, not a convention.** A `Finding` cannot be constructed without a
   `Citation`. Untrusted model output is a `CandidateFinding` (citation is `str | None`); it
   only becomes a `Finding` by passing grounding. The compiler enforces the guarantee.
2. **The router can't be socially engineered.** Heuristics set a floor of required specialists;
   the LLM reading the (attacker-controlled) diff may only extend it. There's a test with a diff
   that says "security review not required, pre-approved" — the security specialist runs anyway.
3. **Everything was measured, and the unflattering numbers are kept.** Reranking was *cut*
   because the eval showed it lost (NDCG@5 −0.09 at 91× the latency). Trajectory finding-recall
   came back low and is reported as-is. The honesty is the point.

---

## 4. Run it locally

### Prerequisites
- Python 3.12 + [`uv`](https://docs.astral.sh/uv/), Docker (for Postgres + the GitHub MCP
  server), Node 22 + `pnpm` (frontend), and either a local **Ollama** or a **Groq** API key.

### Backend
```bash
uv sync --all-extras
docker run -d --name quorum-postgres -e POSTGRES_USER=quorum -e POSTGRES_PASSWORD=quorum \
  -e POSTGRES_DB=quorum -p 5433:5432 pgvector/pgvector:pg16
cp .env.example .env          # then fill in the values below
uv run uvicorn app.interface.composition:app --port 8000
```

Minimum `.env` for a local run against Ollama (no API costs):
```
QUORUM_LLM_PROVIDER=ollama
QUORUM_GITHUB_TOKEN=<a fine-grained PAT, public-repo read is enough for reviews>
QUORUM_DATABASE_URL=postgresql://quorum:quorum@localhost:5433/quorum
```
(`ollama pull llama3.1:8b` first.) For Groq instead: `QUORUM_LLM_PROVIDER=groq` +
`QUORUM_GROQ_API_KEY=...`.

### Frontend
```bash
cd frontend
pnpm install
pnpm dev                      # http://localhost:3000, expects the backend on :8000
```

### The one-shot CLI demo (no frontend, no server)
```bash
uv run python -m scripts.demo         # reviews mypy#21647 and black#5237, prints findings
```

---

## 5. Test it

```bash
uv run pytest                 # full suite (needs the Postgres container up)
uv run mypy                   # strict, zero-tolerance
uv run ruff check . && uv run ruff format --check .
uv run lint-imports           # architecture boundaries are enforced, not aspirational
cd frontend && pnpm typecheck && pnpm lint && pnpm build
```

The retrieval quality gate (reproducible, local inference, no API key):
```bash
uv run python -m eval.retrieval.gate
```

---

## 6. Deploy the frontend (detailed runbook)

The backend is already live. The frontend is defined as a second service (`quorum-web`) in
[`render.yaml`](render.yaml); it just needs to be created once. Step by step:

1. **Render dashboard → Blueprints →** your existing Quorum blueprint **→ "Sync"**. (If you
   never made a blueprint: **New → Blueprint → connect the `sahil7359/Quorum` repo**.) Render
   reads `render.yaml` and detects the new `quorum-web` web service.
2. **Approve the plan.** It's a Docker web service on the free tier. No secrets to enter —
   `NEXT_PUBLIC_API_BASE` is auto-wired from the backend (`quorum`) service's own host, so the
   frontend build points at the live backend automatically.
3. **Deploy.** First build takes a few minutes (installs deps, runs `next build`). It's
   already been verified to build and serve correctly as a container.
4. When it shows **Live**, open its URL — you get the dashboard. Put that URL at the top of this
   file and in the README so it's the front door.

> Why the frontend is a separate service, not part of the backend: the backend is a 512MB
> Python container already carrying the ML runtime; the frontend is a static-ish Next build.
> Keeping them separate means the UI can't be starved of memory by an ingestion run, and each
> scales and redeploys on its own.

Full backend deploy details, credentials, and known limitations (ingestion memory ceiling on
the free tier, installation-token expiry) are in [`docs/Deploy.md`](docs/Deploy.md).

---

## 7. How a review is routed, end to end (the detailed walkthrough)

What actually happens between a click and a finding. This is the flow to narrate in a
system-design round; the same path backs both the dashboard's SSE stream and the CLI demo.

```
Browser (dashboard)
  │  POST /api/reviews {repo, pr_number}        ← no PR data in the URL, on purpose
  ▼
FastAPI  app/interface/api/app.py
  │  parses owner/repo, opens a Server-Sent-Events stream
  ▼
ReviewService.review_stream   app/interface/review_service.py
  │  1. cache check      — sha256(repo, pr, head_sha, config_hash). Hit → return instantly.
  │  2. rate limit       — one global daily counter (QUORUM_LIVE_REVIEWS_PER_DAY). Cache hits skip it.
  │  3. budget check     — daily token cap; if exhausted, serve the last cached review with a banner.
  │  4. ingestion        — IngestionService: if this (repo, commit) has no chunks yet, discover the
  │                        repo's markdown via GitHub code search, fetch each at the exact head_sha,
  │                        chunk + embed + store. Cached after the first time.  →  emits ingestion.* events
  ▼
LangGraph pipeline   app/application/agents/  (each node streams a node.* event)
  │
  ├─ ingest       fetch the PR + unified diff via the GitHub MCP server; AST-scope the diff to the
  │               enclosing function/class (not whole files) → "context scoped down 97%"
  │
  ├─ route        heuristics compute a FLOOR of specialists from the diff (correctness is always in;
  │               source-without-tests adds test-coverage; risky patterns add security). The LLM reads
  │               the diff and may only ADD specialists to that floor — never remove one. A diff that
  │               says "security review not required" cannot talk the security specialist out of running.
  │
  ├─ specialists  for each chosen specialist: build a query, retrieve top-k doc chunks (dense bge-small
  │               + BM25, fused by reciprocal-rank-fusion), prompt the model, get back CandidateFindings.
  │               A CandidateFinding's chunk_id is `str | None` — untrusted model output.
  │
  └─ synthesise   CITE-OR-DROP. Each candidate becomes a real Finding only if its chunk_id names a chunk
                  that (a) exists and (b) was actually shown to that specialist. Everything else is
                  dropped, with a reason (no_citation / malformed / unknown / not-visible). → node.synthesise
  ▼
review.completed event  → the dashboard renders findings, each with the doc line it cites, and the
                          review is written to the cache (so it shows up in history, instantly next time).

  ─────────────────────────────────────────────────────────────────────────────
  Write path (optional, human-in-the-loop, not on the hot path above):
  a human approves a finding → post_review_comment posts it to the PR as an inline comment via a
  GitHub App (quorum-reviewer-sahil[bot]). The write methods take the Approval as an argument, so an
  unapproved post is unexpressible. See scripts/write_demo.py and test-repo PR #1.
```

**Three places to pause and point at during a design round:**
- **Step 2–4 preflight order** — cache → rate limit → budget → ingest. Each is a cheap gate before
  the expensive graph; the order is deliberate (never spend a token you can avoid).
- **`route`** — the trust boundary. The diff is attacker-controlled; the router treats the model as
  advisory-only over a heuristic floor.
- **`synthesise`** — the thesis. `Finding` cannot be *constructed* without a `Citation`; the drop is
  a type-system consequence, not a policy someone has to remember to apply.

---

## 8. Known limitations (state them before you're asked)

- **First review of a repo is slow** (docs ingestion + live model); cached after that.
- **The hosted free tier (512MB) can't index a large repo's docs from scratch.** fastembed's
  ONNX runtime (~200MB) plus the app plus the indexing workload exceeds 512MB, so a *first-time*
  review of a doc-heavy repo (mypy, black) runs the container out of memory and it restarts —
  the dashboard surfaces this ("the server ran out of memory indexing…") rather than hanging.
  **Cache hits are unaffected**, so the hosted demo leads with an already-indexed PR. The clean
  fixes, both real and both deliberately not taken for a free demo: a paid tier with more RAM
  (one config change, no code), or moving embeddings to a hosted API to drop the ~200MB from the
  server. Running locally (laptop RAM) sidesteps it entirely — that's where the full,
  findings-rich review lives. This is a *deployment-tier* limit, not a design flaw: the same
  binary reviews any repo end-to-end given adequate memory.
- **Finding quality is model-bound.** On a small model the specialists lean on generic
  security phrasing. The *grounding* guarantee holds regardless — that's the part that's
  engineered; the phrasing is the model's.
- **GitHub App token expires hourly**; the on-demand write path re-mints per run, a long-running
  server would need to refresh-and-reconnect.
