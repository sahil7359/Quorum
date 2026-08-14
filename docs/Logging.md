# Quorum — Log Event Catalogue

Every structured event Quorum emits, with **the question it exists to answer**. If an event
cannot be tied to a question I will actually ask, it is noise that looks like signal and it
does not belong here.

Names are declared in `app/domain/log_events.py`. `test_every_log_event_is_documented`
asserts that every constant there appears in this file, and
`test_no_undeclared_log_events_are_emitted` asserts that no call site uses a bare string —
so this catalogue cannot silently drift from the code.

## Rules that apply to every line

- **Structured JSON to stdout.** No log files; Render captures stdout.
- **`run_id` on every event**, bound once via `logger.bind(run_id=...)`, so one review
  reconstructs end to end from logs alone.
- **Telemetry never fails a request.** Every emit path swallows its own exceptions.
- **Never log secrets, tokens, or raw diff content at INFO.** A diff is attacker-controlled
  and may itself contain a credential — logging it would *create* the incident we guard
  against. Redaction happens before the call, not inside it.
- **Log decisions, not just events.** `chose security specialist` is nearly useless.
  `chose security specialist: diff touches app/auth/, 2 path globs matched` is debuggable.

## Levels

| Level | Meaning |
| --- | --- |
| ERROR | Broke. Needs a human. |
| WARN | Degraded but handled — fallback used, retry fired, specialist dropped. |
| INFO | State transitions and decisions. One line per graph node. Reads as a narrative. |
| DEBUG | Payloads and prompts. Off in production. |

---

## Run lifecycle

| Event | Level | Why it exists | Key fields |
| --- | --- | --- | --- |
| `run.started` | INFO | Anchors every other line for this review; without it a `run_id` in a later event has no origin. | `repo`, `pr_number`, `head_sha`, `config_hash` |
| `run.completed` | INFO | Gives wall-clock duration and the final finding count — the two numbers I check first. | `status`, `findings`, `duration_ms` |
| `run.failed` | ERROR | A review that produced nothing must be distinguishable from a review that found nothing. Those look identical in a UI and are opposite problems. | `error`, `node` |

## Graph nodes

| Event | Level | Why it exists | Key fields |
| --- | --- | --- | --- |
| `node.started` | INFO | One line per node makes the log read as a narrative of the review, which is the fastest way to see where a run went wrong. | `node` |
| `node.completed` | INFO | Per-node duration is how latency gets attributed without a tracing backend. | `node`, `duration_ms` |
| `node.failed` | ERROR | Names the node that broke, so the stack trace has a location before I read it. | `node`, `error` |

Emitted by `TracedNode` in the base class, **never by a node itself** — so a node that is
not traced cannot be added. Enforced by `test_every_node_is_traced`.

## Routing — the most important line in the system

| Event | Level | Why it exists | Key fields |
| --- | --- | --- | --- |
| `route.decided` | INFO | **Specialist routing accuracy is a published metric and cannot be debugged without this.** It records not just *which* specialists ran but *why*, split into the heuristic floor and what the LLM added, so a bad call is attributable to one or the other rather than to "the router". | `specialists`, `reason`, `heuristic_floor`, `llm_added`, `signals` |
| `route.llm_removal_ignored` | WARN | The LLM may only *extend* the heuristic floor. When it tries to remove a specialist we ignore it — and record that, because a model repeatedly trying to skip the security reviewer is a prompt problem I want to see. | `attempted_removal` |
| `route.llm_unparseable` | WARN | Falling back to the heuristic floor is correct behaviour, but a silent fallback hides a degrading prompt. | `raw_excerpt` |

## Retrieval

| Event | Level | Why it exists | Key fields |
| --- | --- | --- | --- |
| `retrieval.completed` | INFO | Answers "why did the specialist cite *that*?". Records the chunk ids returned, whether reranking ran, and how many survived — the inputs to every grounding decision downstream. | `query_chars`, `dense_hits`, `sparse_hits`, `fused`, `reranked`, `survivors` |

`query_chars` rather than the query itself: specialist queries are built from diff content,
which is untrusted and must not reach a log line at INFO.

## Specialists and findings

| Event | Level | Why it exists | Key fields |
| --- | --- | --- | --- |
| `specialist.started` | INFO | Marks the boundary for per-specialist latency and token attribution. | `specialist` |
| `specialist.completed` | INFO | Candidate count *before* grounding — compared against `finding.raised` this gives the drop rate for free. | `specialist`, `candidates`, `duration_ms` |
| `specialist.failed` | WARN | **WARN, not ERROR**: one specialist returning malformed JSON is handled — it is dropped and the others still produce a review. Escalating it would train me to ignore ERROR. | `specialist`, `reason` |
| `finding.raised` | INFO | One line per surviving finding, naming the chunk that grounds it. This is the audit-adjacent record of what the system actually claimed. | `specialist`, `severity`, `chunk_id`, `confidence` |
| `finding.dropped` | INFO | **The cite-or-drop counter.** "How often does the model try to cite something it was not shown?" is a real quality signal and it is invisible if drops are filtered silently. | `specialist`, `reason` |

`finding.dropped` is INFO rather than WARN deliberately: dropping an uncited finding is the
system working correctly, not a degradation.

## LLM calls

| Event | Level | Why it exists | Key fields |
| --- | --- | --- | --- |
| `llm.called` | INFO | Cost and latency attribution per node. Token counts come from the provider, never from an estimate, because the daily budget is derived by summing these. | `provider`, `model`, `node`, `prompt_tokens`, `output_tokens`, `latency_ms`, `finish_reason` |
| `llm.failed` | WARN | Distinguishes a provider failure from a parse failure. Both surface as "no findings" without this. | `provider`, `model`, `node`, `error` |

Prompts are logged at **DEBUG only** — they contain fenced diff content.

## Context scoping

| Event | Level | Why it exists | Key fields |
| --- | --- | --- | --- |
| `context.scoped` | INFO | Records tokens before and after AST scoping. The token-reduction figure is a published number, so it has to come from recorded fact rather than a one-off measurement. | `files`, `tokens_whole_file`, `tokens_scoped`, `reduction_pct` |

## MCP

| Event | Level | Why it exists | Key fields |
| --- | --- | --- | --- |
| `mcp.connected` | INFO | Confirms which server we actually reached and how many tools it offered. | `server`, `advertised` |
| `mcp.tools.unvetted_available` | INFO | A third-party server gaining a destructive tool should be *visible*, not discovered later. Not an error — the real server legitimately exposes dozens we do not use. | `server`, `count`, `sample` |
| `mcp.tool.call` | DEBUG | Argument names only, never values: arguments can carry file contents. | `tool`, `arguments` (keys) |
| `mcp.tool.refused` | ERROR | Something reached for a tool it is not allowed to use. That is either a bug or an attack, and both need a human. | `tool`, `reason` |

## Diff handling

| Event | Level | Why it exists | Key fields |
| --- | --- | --- | --- |
| `diff.truncated` | WARN | A silently truncated review is a lie about its own coverage. WARN because it is degraded but handled. | `repo`, `pr`, `limit`, `files` |

## Cost controls

| Event | Level | Why it exists | Key fields |
| --- | --- | --- | --- |
| `cache.hit` | INFO | **So a suspiciously cheap run can be explained.** A review that returns in 40ms having spent no tokens is either a cache hit or a bug. | `cache_key` |
| `cache.miss` | INFO | The other half of the same question. | `cache_key` |
| `budget.reserved` | INFO | Tokens consumed against the daily cap, so quota exhaustion is visible *before* it bites. | `estimated`, `consumed`, `limit` |
| `budget.exhausted` | WARN | The moment behaviour changes to serving cached results. Must never be silent — an honest banner in the UI depends on this having happened. | `consumed`, `limit` |
| `rate_limit.exceeded` | WARN | Distinct from the token budget on purpose: this fires on request *volume* against `QUORUM_LIVE_REVIEWS_PER_DAY`, not spend. A cache hit never reaches this check. | `repo`, `pr_number` |

## Approval and publish

| Event | Level | Why it exists | Key fields |
| --- | --- | --- | --- |
| `approval.proposed` | INFO | The graph has stopped and is waiting for a human. Pairs with the audit row. | `findings` |
| `approval.decided` | INFO | Who decided what, and when. The **log** copy is for debugging; the **audit table** is the durable record. | `finding_id`, `action`, `actor` |
| `publish.posted` | INFO | What actually reached GitHub, with the grounding chunk. | `finding_id`, `chunk_id`, `comment_id` |
| `publish.refused` | ERROR | The last guard fired. Either a bug or an attempt to bypass approval; either way a human needs to look. | `finding_id`, `reason` |

## Why logs, traces and audit are three things

| | Purpose | Reader | Retention |
| --- | --- | --- | --- |
| **Logs** | Debug a specific failure | Me, at 1am | Short, sampled |
| **Traces** | Attribute latency and cost across a run | Me, tuning | Medium |
| **Audit** | Prove what the agent did and who approved it | Anyone asking "why did it post that?" | Long, immutable, never sampled |

Conflating them is the common mistake. If the approval trail is a log stream it gets rotated
away exactly when someone needs it — hence `audit_events` is a **table** with database-level
append-only rules, not a logger call. `approval.decided` above is the *debugging* copy; it is
not the record of record.
