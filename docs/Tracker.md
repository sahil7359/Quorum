# Quorum — Status Board

Kept current as work proceeds. Last updated: **2026-08-14**.

> **Run stopped after Phase 3.** Planning + Phases 0-3 complete and green. Phases 4-7 not
> started -- remaining context would have forced a rushed Phase 4. See HANDOFF.md.

Legend: ✅ done · 🟡 in progress · ⬜ not started · ⚠ done with a caveat

## Phases

| # | Phase | Status | Commit | Tests | learn/ | ADRs |
| --- | --- | --- | --- | --- | --- | --- |
| 0 | Scaffolding | ✅ | `phase/00-scaffolding` | 25 passed | ✅ `00-scaffolding.md` | 0001, 0002 |
| 1 | Domain core | ✅ | `phase/01-domain-core` | 133 passed | ✅ `01-domain-and-ports.md` | — |
| 2 | MCP client | ✅ | `phase/02-mcp-client` | 196 passed | ✅ `02-mcp-client.md` | 0003 |
| 3 | Document RAG | ✅ | `phase/03-document-rag` | 257 passed | ✅ `03-retrieval.md` | 0004 |
| 4 | Specialists + supervisor | ⬜ | — | — | `04-multi-agent.md` | — |
| 5 | HITL + audit | ⬜ | — | — | `05-hitl-and-audit.md` | — |
| 6 | Trajectory eval | ⬜ | — | — | `06-trajectory-eval.md` | — |
| 7 | MCP server | ⬜ | — | — | `07-mcp-server.md` | — |
| 8 | Serving | ⬜ | — | — | `08-serving.md` | — |
| 9 | Security baseline | ⬜ | — | — | `09-security-baseline.md` | — |
| 10 | Observability | ⬜ | — | — | `10-observability.md` | — |
| 11 | CI | ⬜ | — | — | `11-ci.md` | — |
| 12 | Demo | ⬜ | — | — | `12-demo.md` | — |
| 13 | Deploy + hand-back | ⬜ | — | — | `13-deploy.md` | — |

## Definition of Done

| Item | Status | Note |
| --- | --- | --- |
| Live public URL, no setup needed | ⬜ | Phase 13 |
| MCP consumed | ⚠ | Client built and tested over real stdio against a fake MCP server. **Not yet run against the real GitHub MCP server** — no token. |
| MCP server published | ⬜ | Phase 7 |
| NDCG@5 rerank delta reported honestly | ✅ | **−0.0793** — reranking lost and was cut. ADR-0004 |
| Trajectory eval gating CI vs committed baseline | ⬜ | Phase 6 + 11 |
| HITL approval on every write path + audit log | ⬜ | Phase 5 |
| `learn/00`–`learn/13` complete | 🟡 | 4 of 14 |
| README with real numbers and real limitations | ⬜ | Phase 13 |
| $0/month | ⬜ | verified at Phase 13 |

## Published numbers

Nothing is listed here until it traces to a reproducible run. `TODO: not yet measured` is
the correct entry; a plausible number is not.

| Number | Value | How measured | What it does not prove |
| --- | --- | --- | --- |
| NDCG@5 (hybrid) | **0.5811** | 20 golden queries, frozen corpus, `eval/baselines/retrieval.json` | Labels are self-written; NDCG rewards ordering Quorum does not need |
| Recall@5 (hybrid) | **0.6283** | as above, normalised by `min(len(relevant), 5)` | Same label caveat |
| Success@5 (hybrid) | **0.9500** | any relevant chunk in top 5 | Closest metric to Quorum's real need; still self-labelled |
| Rerank delta NDCG@5 | **−0.0793** | hybrid+rerank minus hybrid | Only on this corpus with this model pair |
| Finding precision | TODO: not yet measured | Phase 6 trajectory eval | — |
| Finding recall | TODO: not yet measured | Phase 6 trajectory eval | — |
| Specialist routing accuracy | TODO: not yet measured | Phase 6 trajectory eval | — |
| Token reduction, AST-scoped vs whole-file | TODO: not yet measured | Phase 4 | — |
| Cost per review | TODO: not yet measured | Trace aggregation | — |

## Environment facts established at project start (2026-08-14)

| Fact | Value | Consequence |
| --- | --- | --- |
| `GROQ_API_KEY` | **not set** | No live Groq calls this run. Provider abstraction built and exercised against Ollama + fakes. |
| `GITHUB_TOKEN` | **not set** | GitHub MCP server cannot authenticate. Client tested against a fake MCP server over real stdio. Unauthenticated REST (60 req/hr) is available for building fixtures. |
| Ollama | running, GPU RTX 5070 Ti 16GB | Local inference genuinely available: `llama3.1:8b`, `qwen3-coder:30b`, `nomic-embed-text`. |
| Docker | daemon up | `pgvector/pgvector:pg16` usable for integration tests. |
| Network | reachable (pypi, api.github.com) | Dependency install and fixture fetching work. |
