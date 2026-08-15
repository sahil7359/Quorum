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

## What's required to deploy for real

| Credential | Where it goes | Notes |
| --- | --- | --- |
| GitHub fine-grained PAT, `public_repo` scope | `QUORUM_GITHUB_TOKEN` | Same token Phase 2/6/12 already used locally. Read-only is enough -- this deploy exercises no write path. |
| Groq API key | `QUORUM_GROQ_API_KEY` | **Never verified against a real key.** First live call should be treated as a smoke test, not assumed to work -- see HANDOFF.md's credential-blocked items. |
| Neon Postgres connection string, `pgvector` extension enabled | `QUORUM_DATABASE_URL` | Plain `postgresql://...`, not `postgresql+psycopg://...` -- the `+psycopg` scheme is SQLAlchemy convention and `psycopg.connect()` cannot parse it (`missing "=" after ...`). Found wiring this exact deploy, not by inspection; fixed in `Settings`'s default and `.env.example`, but a Neon-issued string needs the same check before pasting it in. |
| Render account | hosting | Free tier, 512MB RAM -- the constraint that drove the `fastembed` choice over `sentence-transformers` in the first place. |

## Known limitation: the chunk store starts empty

There is no "ingest this repo's docs" step wired into the production composition root yet.
`scripts/demo.py` (Phase 12) pre-ingests a curated doc set for two specific repos before
reviewing them; the deployed service has no equivalent for an arbitrary repo a real caller
asks about. A review against a repo nobody has ingested doesn't error -- `HybridRetriever` and
its chunk store both treat zero matches as "nothing relevant", the same graceful-degradation
path already proven in Phase 12's `python/mypy` run -- it just cites nothing, because there is
nothing to cite. Deciding how ingestion should actually be triggered (on first request? a
background job? an explicit admin call?) is real, undecided design work, deliberately deferred
in HANDOFF.md (R7) rather than guessed at here.

## Deploying

1. Push `render.yaml` to the repo (already done).
2. In the Render dashboard: New → Blueprint → point at this repo.
3. Fill in the four `sync: false` env vars listed above.
4. First deploy will build the Docker image and start the service. Check `/healthz` then
   `/readyz` -- `/readyz` specifically checks the budget store is reachable, which means a bad
   `QUORUM_DATABASE_URL` shows up as a 503 there rather than a mysterious first-request failure.
