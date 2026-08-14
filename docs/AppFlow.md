# Quorum — Application Flow

*The document I reread when I forget how it fits together.*

One review, end to end. `run_id` is minted at step 2 and appears in every log line, span,
and audit row from there on.

---

### 1. Request arrives

Two doors, one road.

```
POST /api/reviews  {repo: "psf/requests", pr: 6432, live: false}
        ── or ──
MCP  review_pull_request {repo, pr_number}
```

Both land on `ReviewService.review()`. The MCP door **cannot** reach the publish path —
it returns findings only. The web door can, but only through the approval gate.

An `Idempotency-Key` header (HTTP) or `(repo, pr, sha)` (MCP) prevents a double-tap from
running two reviews.

### 2. Mint `run_id`, probe the cache

```
run_id = uuid7()                       # time-ordered, sorts by creation
cache_key = sha256(repo, pr, head_sha, config_hash)
```

`head_sha` comes from a single cheap MCP `get_pull_request` call — we must know the SHA
before we can ask whether we have already reviewed it.

**Cache hit** → return the stored review, emit `cache.hit` with the key, done. Zero tokens.
This is what makes a gallery of six PRs free to serve forever.

**Cache miss** → continue, and emit `cache.miss` with the key, so a suspiciously cheap run
is always explainable.

### 3. Budget pre-flight

`BudgetService.reserve(estimated_tokens)`.

If the daily cap is already spent, we do **not** attempt a degraded live review. We serve
the most recent cached review for that PR with an honest banner — *"daily budget exhausted,
showing a cached review from <date>"* — or, if nothing is cached, we say so plainly.
Emits `budget.exhausted` with tokens consumed against the cap.

### 4. `ingest` node — fetch the PR

Through the GitHub MCP client, allowlist-checked:

- `get_pull_request` → title, body, author, base/head SHA
- `get_pull_request_files` → changed paths, per-file patch, additions/deletions
- `get_pull_request_diff` → unified diff

Then, immediately:

- **Cap the diff.** Over `QUORUM_MAX_DIFF_LINES` → truncate, and record that truncation in
  the review output. A silently truncated review is a lie about coverage.
- **Fence the diff as untrusted.** Diff content is attacker-controlled. It is wrapped in
  an explicit data delimiter and is never interpolated into a system prompt.
- **Redact before logging.** The diff never reaches a log line at INFO.

### 5. `route` node — the supervisor decides

Heuristics run first and are always trusted:

```python
signals = {
    "paths_touching_security": ["app/auth/login.py"],  # glob match
    "source_changed_without_tests": True,
    "added_lines": 213,
    "new_public_symbols": ["authenticate_user"],
}
```

Heuristics produce a floor set. The LLM is then asked to *confirm or extend* it, never to
remove from it. `correctness` is unconditional.

Emits **the single most important log line in the system**:

```json
{"event":"route.decided","run_id":"…","specialists":["correctness","security"],
 "reason":"diff touches app/auth/; 2 security path globs matched; 213 added lines",
 "heuristic_floor":["correctness","security"],"llm_added":[],"llm_removed_ignored":[]}
```

Routing accuracy is a published metric. It cannot be debugged without this line.

### 6. Specialist nodes — retrieve, then review

Sequential by default (the 12K tokens/min ceiling makes parallel dispatch trip the limit).
Each specialist, independently:

**a. Build a retrieval query** from its own concern plus the changed symbols —
`"authentication token expiry convention"`, not the whole diff.

**b. Retrieve** (`HybridRetriever`):

```
dense(bge-small, cosine) → 30 ┐
                              ├─ RRF fuse → 30 → cross-encoder rerank → top 5
BM25(code-aware tokenizer) → 30 ┘
```

Emits `retrieval.completed` with query, chunk ids, scores, whether reranked, survivors.

**c. Review.** Prompt carries: the specialist's role, the fenced diff hunks scoped to the
changed regions by AST, and the 5 retrieved chunks **with their chunk ids**. The model is
required to return structured findings, each naming the `chunk_id` it relies on.

**d. Parse.** Malformed JSON → the specialist is dropped, `specialist.failed` logged at
WARN, the other specialists still produce a review. One bad actor does not fail the run.

### 7. `synthesise` node — merge and enforce grounding

Runs on the 70B model, once.

1. **Cite-or-drop.** Any finding whose `chunk_id` is absent, unresolvable, or was not in
   the set returned to that specialist is **dropped**. In code, at this boundary — not
   requested in a prompt. Prompts are advisory; code is not.
2. Deduplicate findings that three specialists all noticed.
3. Rank by severity × confidence.
4. Emit `finding.raised` per survivor: specialist, grounding chunk id, confidence.

The output is a `Review` of `Finding`s, each with a `Citation` resolving to
`(file, section, offset)` in the target repo.

### 8. `interrupt()` — the graph stops

```python
decision = interrupt({"findings": [...]})  # durable, Postgres-checkpointed
```

State is checkpointed to Postgres. **The process may now die.** A free-tier Render instance
sleeps; a review proposed at 14:00 and approved at 19:00 resumes in a different process
with the same state. This is why the checkpointer is durable and not in-memory.

An audit row is written per finding: `action=proposed`.

Meanwhile, SSE has been streaming the review as it formed — routing decision, each
specialist starting and finishing, each finding as it survives synthesis — so the human is
reading findings before the graph reaches the gate.

### 9. Human decides

Per finding: **approve** · **edit** · **reject**. Each writes an immutable audit row with
actor and timestamp. Nothing is implicit; not deciding is not approval.

### 10. `publish` node — the only write

For each **approved** finding, and only those:

1. Re-check the audit log for an `approved` row matching that exact finding and payload
   hash. Belt and braces: the graph already routed here, but the guard does not trust the
   graph.
2. `add_pull_request_review_comment` via the GitHub MCP client, with the citation rendered
   as a link into the target repository's docs.
3. Write `action=posted` with the GitHub comment id.

If step 1 fails, the write is refused and logged at ERROR. `test_publish_requires_approval_row`
covers this, and it has been proven to fail by deliberately removing the guard.

### 11. Persist and cache

The completed review is stored under the cache key from step 2. The next request for the
same `(repo, pr, head_sha, config_hash)` costs zero tokens.

---

## Failure paths

| Failure | Behaviour |
| --- | --- |
| MCP server unreachable | Fail the run at `ingest`, ERROR, no partial review |
| Diff over cap | Truncate + banner in output, WARN |
| Budget exhausted | Serve cached with honest banner, or refuse; WARN |
| One specialist returns malformed JSON | Drop that specialist, continue, WARN |
| **All** specialists fail | Fail the run; an empty review is not a clean review |
| Retrieval returns nothing | Specialist produces no findings — cite-or-drop guarantees it cannot invent one |
| Human never responds | Review stays `proposed` forever; nothing is posted. Correct behaviour, not a bug |
| Telemetry backend down | Swallowed. Telemetry never fails a request |
