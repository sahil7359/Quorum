# Deploy

Backend only. There is no frontend yet (`frontend/` is empty), so this covers the FastAPI
service (`app.interface.composition:app`) on Render. See [`render.yaml`](../render.yaml) for
the blueprint this document explains.

## What's already verified

- `docker build .` produces a working image (CI's `container-build` job checks this on every
  push, but only that it *builds* -- it has never been run).
- Running that image **without** Docker access -- the actual condition on Render -- was tested
  directly: `docker run` with no `docker` binary inside the container reproduces the exact
  failure a naive deploy would hit (`GitHubMcpClient`'s default launch command is `docker run
  ghcr.io/github/github-mcp-server`, which needs a daemon nothing in the runtime image has).
  The Dockerfile now vendors the MCP server's own binary
  (`ghcr.io/github/github-mcp-server`'s `/server/github-mcp-server`) instead, and `render.yaml`
  points `QUORUM_GITHUB_MCP_COMMAND`/`QUORUM_GITHUB_MCP_ARGS` at it. Verified by actually
  running the rebuilt image the same way, with the same missing Docker access, and watching it
  start and answer `/healthz` and `/readyz` correctly.
- `/healthz`, `/readyz`, and a real `POST /api/reviews` request were exercised against a
  locally-run copy of `app.interface.composition:app` (real GitHub MCP server, real Ollama) --
  see LEARN.md's Phase 13 entry for what that run actually returned.
- **Deployed for real, at `https://quorum-aka2.onrender.com`, with all three credentials
  live.** `/healthz` and `/readyz` both green against the actual Render deployment, and a real
  `POST /api/reviews` against `psf/black#5280` ran end to end with `failed_specialists: []` --
  the Groq adapter's first-ever call against a real key, succeeding on the first try. See
  LEARN.md's "live deploy verification" entry.

## What's required to deploy for real

| Credential | Where it goes | Notes |
| --- | --- | --- |
| GitHub fine-grained PAT, `public_repo` scope | `QUORUM_GITHUB_TOKEN` | Same token Phase 2/6/12 already used locally. Read-only is enough -- this deploy exercises no write path. |
| Groq API key | `QUORUM_GROQ_API_KEY` | Verified live against the actual deployment -- see above. |
| Neon Postgres connection string, `pgvector` extension enabled | `QUORUM_DATABASE_URL` | Plain `postgresql://...`, not `postgresql+psycopg://...` -- the `+psycopg` scheme is SQLAlchemy convention and `psycopg.connect()` cannot parse it (`missing "=" after ...`). Found wiring this exact deploy, not by inspection; fixed in `Settings`'s default and `.env.example`. Verified live -- `/readyz` (checks the budget store) has returned `ready`, not 503, against the real Neon connection. |
| Render account | hosting | Free tier, 512MB RAM -- the constraint that drove the `fastembed` choice over `sentence-transformers` in the first place. |

## Ingestion

Resolved -- `IngestionService` (see `app/interface/ingestion_service.py`) answers a question
earlier phases deliberately left open: on the first review that asks about a given
`(repo, commit_sha)`, it discovers the repo's own markdown docs via GitHub code search
(`extension:md repo:owner/name`, one API call, verified live against `psf/black`), fetches each
at the review's exact commit, chunks, embeds, and stores them -- inline with that first
request, not a background job or an admin endpoint. Every later review at the same commit skips
straight past this (`ChunkStorePort.all_for_repo` already has rows). A repo whose docs can't be
listed or fetched still lets the review run, with nothing to cite that time -- the same
graceful-degradation path `HybridRetriever` and its chunk store already had.

## Deploying

1. Push `render.yaml` to the repo (already done).
2. In the Render dashboard: New → Blueprint → point at this repo.
3. Fill in the four `sync: false` env vars listed above.
4. First deploy will build the Docker image and start the service. Check `/healthz` then
   `/readyz` -- `/readyz` specifically checks the budget store is reachable, which means a bad
   `QUORUM_DATABASE_URL` shows up as a 503 there rather than a mysterious first-request failure.
