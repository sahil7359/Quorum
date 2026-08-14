# Quorum — Product Requirements

Status: living document. Owner: Sahil Chakraborty.

## 1. Problem

Code review on a busy repository fails in two directions at once.

Human reviewers are the bottleneck: they are slow, inconsistent between people, and
they miss things late on a Friday. Existing LLM review bots fail differently — they are
fast and confident and *ungrounded*. They flag style opinions as defects, they invent
conventions the repository does not hold, and they post directly to the pull request
with no human in the loop. A bot that comments "you should use dependency injection here"
on a repository whose own `ARCHITECTURE.md` explicitly rejects a DI container is worse
than no bot, because it costs a human a context switch to dismiss.

The gap is not "an LLM that reads diffs". The gap is **a reviewer whose every claim is
traceable to the repository's own written rules, and which cannot act without a human
saying yes.**

## 2. What Quorum is

A supervisor agent that:

1. Reads a pull request through the **official GitHub MCP server** (files, diff, metadata).
2. Decides which specialist reviewers the diff actually warrants, and logs why.
3. Dispatches those specialists — **correctness, security, test-coverage** — each of which
   must ground its findings in retrieved chunks of the target repository's own documentation.
4. Synthesises the findings, deduplicates, and drops anything without a citation.
5. **Stops** and waits for a human to approve, edit, or reject each finding.
6. Only then writes back to GitHub, and records what it did in an append-only audit table.
7. Exposes the whole capability as an **MCP server**, so any MCP client can call it.

## 3. Why this project exists

My previous project (DataChat, a LangGraph text-to-SQL agent) already demonstrates:
typed clean architecture, evaluation gates in CI, guardrail chains, and a deployed
zero-cost service. It does **not** demonstrate three things, and those three things are
the entire reason Quorum exists:

| Capability | Why it matters |
| --- | --- |
| **MCP — consumed and published** | Consuming a third-party MCP server is now table stakes. *Publishing* one is the rarer half of the skill and almost nobody has done it. |
| **Multi-agent orchestration** | A supervisor that routes to specialists and merges their output, with the routing decision itself measured. |
| **Document RAG with citations** | Every finding cites a chunk id traceable to `(file, section, offset)`. Grounding is the product, not a feature. |

Everything else in this repository exists to make those three credible.

## 4. Explicit non-goal: security is a baseline, not the showcase

DataChat already goes deep on security: a `sqlglot` AST guardrail chain, a read-only
database role, OWASP LLM Top 10 and Agentic Top 10 mapped to passing tests, and 100% of a
30-case injection corpus blocked.

In Quorum, security is **baseline hygiene, mapped to tests, and then left alone**:

- Tool sandboxing (allowlisted MCP tools, no shell, no filesystem writes from agent code).
- Scoped tokens (`public_repo` only; never a classic full-scope PAT).
- **Diff content is untrusted, attacker-controlled input** and is fenced and never
  interpolated into a system prompt.
- Secrets hygiene (`gitleaks` in pre-commit and CI, redaction before any log line).

There is a `security` **specialist agent** — that is a *product feature*, a reviewer role.
It is not the same thing as this project being a security showcase. Do not expand the
baseline into a second security project.

## 5. Users

| User | Need | How Quorum serves it |
| --- | --- | --- |
| **Interviewer / reviewer of this repo** (primary) | Judge whether I can build a grounded, evaluated, multi-agent system | Live URL, a gallery of six real PRs reviewed, published metrics with honest caveats, `learn/` notes |
| **A maintainer of a busy repo** (the pretended user) | Cheap first-pass review that never invents conventions and never posts unsupervised | Cited findings, human approval gate, audit trail |
| **An MCP client** (Claude Desktop, an IDE, another agent) | Call "review this PR" as a tool | Published MCP server with a documented tool schema |

## 6. Scope — v1

**In scope**

- Three specialists only: `correctness`, `security`, `test-coverage`.
- Supervisor routing with a logged, measurable rationale.
- Document RAG over the target repository's own docs, hybrid dense + BM25, optional
  cross-encoder rerank, chunk-level citations.
- MCP client consuming the official GitHub MCP server.
- MCP server publishing Quorum's review capability.
- Human-in-the-loop approval on every write path, backed by a durable LangGraph
  `interrupt()` and an append-only audit table.
- Trajectory evaluation against merged PRs that carry real human review comments.
- FastAPI service with SSE streaming, idempotency keys, rate limiting.
- A gallery of six pre-ingested repositories, reviews cached by commit SHA.
- One rate-limited live review button.

**Out of scope for v1**

- A documentation/style specialist. Four specialists is 4× cost and latency, and the
  documentation reviewer produces the most noise. It gets added only if the eval shows it
  earning its place.
- On-demand ingestion of arbitrary repositories. Unbounded cost on a free tier.
- Private repositories. Public GitHub only.
- Non-GitHub forges.
- Fine-tuning anything.
- Multi-turn conversation with the reviewer. One review, one result.
- Autonomous merge, autonomous approval, or any write that a human did not confirm.

## 7. Definition of Done

- [ ] Live public URL, usable with no setup.
- [ ] MCP consumed **and** an MCP server published.
- [ ] NDCG@5 delta for reranking reported honestly, including if reranking loses.
- [ ] Trajectory eval gating CI against a committed baseline.
- [ ] HITL approval on every write path, with an audit log.
- [ ] `learn/00`–`learn/13` complete.
- [ ] README with real numbers and real limitations.
- [ ] $0/month.

## 8. Success metrics

Every one of these must trace to a run that can be reproduced. A `TODO: not yet measured`
is always preferable to a plausible number.

| Metric | Definition | Where measured |
| --- | --- | --- |
| Finding precision | Findings Quorum raised that a human reviewer also raised on the same merged PR | `eval/` trajectory suite |
| Finding recall | Human review comments Quorum also raised — against an **imperfect ceiling**, since human reviewers miss things too | `eval/` trajectory suite |
| Specialist routing accuracy | Supervisor's chosen specialist set vs. the labelled expected set | `eval/` trajectory suite |
| Tool-call correctness | Fraction of MCP tool calls that were well-formed and necessary | `eval/` trajectory suite |
| NDCG@5 / Recall@5 | Retrieval quality, with and without cross-encoder reranking | `eval/` retrieval suite |
| Citation rate | Fraction of surfaced findings carrying a resolvable chunk id — target 1.00 by construction | Unit + eval |
| Cost per review | Tokens in/out × published price, and steps per review | Trace aggregation |

## 9. Constraints

- **$0/month.** Vercel, Render, Neon Postgres + pgvector, GitHub public repos, Groq free tier.
- Groq free tier on `llama-3.3-70b-versatile`: 30 RPM · 1,000 req/day · 12K tokens/min ·
  100K tokens/day. One review is roughly 25–50K tokens, so **2–4 live reviews per day**,
  and the per-minute ceiling means specialists **cannot run fully in parallel**.
- Evaluation runs locally against Ollama on an RTX 5070 Ti, **never** against Groq. A
  20-PR eval is 500K+ tokens and would exhaust the daily quota in a single run.
- Build time: ~2–3 hours on a weekday, 5–8 across a weekend. Optimise for shipped increments.

## 10. Risks

| Risk | Mitigation |
| --- | --- |
| Groq quota exhausted mid-demo | Cache by commit SHA; global daily budget; honest fallback message to cached result |
| Retrieval returns plausible-but-wrong chunks, so findings are "grounded" in the wrong rule | Retrieval eval with published NDCG@5; citations rendered in the UI so a reader can check |
| Human review comments are a noisy label set | Stated openly in `learn/06`; recall is explicitly measured against an imperfect ceiling |
| Multi-agent sprawl becomes a liability | Three specialists, locked. Adding a fourth requires eval evidence |
| Prompt injection via diff content | Diff is fenced as untrusted data, never interpolated into system prompts; covered by tests |
