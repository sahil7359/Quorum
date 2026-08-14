# Quorum — Data Model

Postgres (Neon) + `pgvector`. All timestamps `timestamptz`, UTC. All ids are application-
generated so they are stable across environments and reproducible in fixtures.

## 1. Overview

```
repos ──< documents ──< chunks
                          │
runs ──< findings >───────┘ (citation)
  │         │
  │         └──< approvals
  └──< audit_events (append-only)
  └──< token_usage
review_cache (keyed by commit SHA)
```

## 2. `chunks` — the table that must be right

This is the one schema decision that cannot be corrected later: **chunk ids are chunk-level
and traceable to `(file, section, offset)`.** File-level ids would silently invalidate every
retrieval number this project publishes.

```sql
CREATE TABLE chunks (
    chunk_id        TEXT PRIMARY KEY,       -- 16 hex chars, see §2.1
    repo            TEXT        NOT NULL,   -- "psf/requests"
    commit_sha      TEXT        NOT NULL,   -- ingest pin — chunks are per-commit
    file_path       TEXT        NOT NULL,   -- "docs/architecture.md"
    section_path    TEXT        NOT NULL,   -- "Design > Retrieval > Hybrid search"
    heading_level   SMALLINT    NOT NULL,
    start_offset    INTEGER     NOT NULL,   -- byte offset into the file
    end_offset      INTEGER     NOT NULL,
    start_line      INTEGER     NOT NULL,   -- for rendering a GitHub deep link
    end_line        INTEGER     NOT NULL,
    ordinal         INTEGER     NOT NULL,   -- position within the section, 0-based
    token_count     INTEGER     NOT NULL,
    content         TEXT        NOT NULL,
    content_sha     TEXT        NOT NULL,   -- dedupe identical chunks across commits
    embedding       VECTOR(384),            -- bge-small-en-v1.5
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (repo, commit_sha, file_path, start_offset, end_offset)
);

CREATE INDEX chunks_embedding_idx ON chunks
    USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_repo_sha_idx ON chunks (repo, commit_sha);
```

### 2.1 Chunk id derivation

```python
locator = f"{repo}@{commit_sha}:{file_path}#{section_path}@{start_offset}-{end_offset}"
chunk_id = hashlib.sha256(locator.encode()).hexdigest()[:16]
```

**Invariants, each with a test:**

| Invariant | Test |
| --- | --- |
| Two chunks from the same file and section but different offsets get different ids | `test_offsets_disambiguate_chunk_ids` |
| Re-ingesting the same commit produces identical ids | `test_chunk_ids_are_stable_across_reingest` |
| A chunk id is *verifiable against* its stored `(file, section, offset)` locator. The id is a hash, not a reversible encoding — resolution runs locator→id, never id→locator | `test_chunk_id_verifies_against_its_locator` |
| Ingesting a different commit produces different ids for the same text | `test_commit_sha_participates_in_chunk_id` |
| No chunk spans more than one file (structurally impossible: a locator names exactly one `file_path`). Asserted against the real chunker in Phase 3 | `test_chunk_never_spans_files` (Phase 3) |

16 hex chars = 64 bits. At the scale of six repositories (order 10⁴ chunks) collision
probability is ~10⁻¹¹. The `UNIQUE` constraint on the locator tuple would catch one anyway.

### 2.2 Chunking strategy

Markdown-aware, splitting on heading boundaries, then packing to a target token count with
overlap. `section_path` is the full heading breadcrumb, which is what makes a citation
readable to a human: *"Design > Retrieval > Hybrid search"* means something; *"chunk 47"*
does not.

Code files are chunked by AST top-level definition where a parser exists, falling back to
fixed-window. Offsets remain byte offsets into the original file either way.

## 3. `repos` and `documents`

```sql
CREATE TABLE repos (
    repo            TEXT PRIMARY KEY,
    default_branch  TEXT        NOT NULL,
    ingested_sha    TEXT,
    ingested_at     TIMESTAMPTZ,
    doc_glob        TEXT        NOT NULL DEFAULT '**/*.md',
    is_gallery      BOOLEAN     NOT NULL DEFAULT false
);

CREATE TABLE documents (
    repo          TEXT NOT NULL REFERENCES repos(repo),
    commit_sha    TEXT NOT NULL,
    file_path     TEXT NOT NULL,
    content_sha   TEXT NOT NULL,
    byte_length   INTEGER NOT NULL,
    chunk_count   INTEGER NOT NULL,
    PRIMARY KEY (repo, commit_sha, file_path)
);
```

## 4. `runs`

```sql
CREATE TABLE runs (
    run_id            UUID PRIMARY KEY,
    repo              TEXT        NOT NULL,
    pr_number         INTEGER     NOT NULL,
    head_sha          TEXT        NOT NULL,
    config_hash       TEXT        NOT NULL,  -- prompt + model + retrieval config
    status            TEXT        NOT NULL,  -- running|proposed|published|failed|rejected
    specialists       TEXT[]      NOT NULL,
    routing_reason    TEXT        NOT NULL,  -- the WHY, persisted not just logged
    diff_truncated    BOOLEAN     NOT NULL DEFAULT false,
    started_at        TIMESTAMPTZ NOT NULL,
    finished_at       TIMESTAMPTZ,
    error             TEXT
);
```

`routing_reason` is persisted, not merely logged, because routing accuracy is a published
metric and logs are sampled and rotated.

## 5. `findings`

```sql
CREATE TABLE findings (
    finding_id     UUID PRIMARY KEY,
    run_id         UUID    NOT NULL REFERENCES runs(run_id),
    specialist     TEXT    NOT NULL,          -- correctness|security|test_coverage
    severity       TEXT    NOT NULL,          -- info|low|medium|high
    confidence     REAL    NOT NULL,          -- 0..1, model-reported
    title          TEXT    NOT NULL,
    body           TEXT    NOT NULL,
    file_path      TEXT,                      -- location in the PR under review
    line_start     INTEGER,
    line_end       INTEGER,
    chunk_id       TEXT    NOT NULL REFERENCES chunks(chunk_id),  -- NOT NULL is the invariant
    payload_hash   TEXT    NOT NULL,          -- sha256 of the exact text proposed
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`chunk_id NOT NULL REFERENCES chunks` makes cite-or-drop a **database constraint**, not
just application logic. An uncited finding cannot be persisted even if the code is wrong.
That is deliberate: I want the invariant enforced in two independent places.

`payload_hash` binds an approval to exact text. Edit the finding after approval and the
hash changes, so the publish guard refuses it.

## 6. `approvals` and `audit_events`

```sql
CREATE TABLE approvals (
    approval_id   UUID PRIMARY KEY,
    run_id        UUID NOT NULL REFERENCES runs(run_id),
    finding_id    UUID NOT NULL REFERENCES findings(finding_id),
    action        TEXT NOT NULL,      -- approved|rejected|edited
    actor         TEXT NOT NULL,
    payload_hash  TEXT NOT NULL,      -- must match findings.payload_hash at publish time
    note          TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

```sql
-- Append-only. Never updated, never deleted, never sampled.
CREATE TABLE audit_events (
    audit_id     BIGSERIAL PRIMARY KEY,
    run_id       UUID        NOT NULL,
    finding_id   UUID,
    action       TEXT        NOT NULL,  -- proposed|approved|rejected|edited|posted|refused
    actor        TEXT        NOT NULL,  -- "system" | user identifier
    payload_hash TEXT,
    detail       JSONB       NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX audit_events_run_idx ON audit_events (run_id, audit_id);

-- Append-only enforced at the database, not merely in application code.
CREATE RULE audit_events_no_update AS ON UPDATE TO audit_events DO INSTEAD NOTHING;
CREATE RULE audit_events_no_delete AS ON DELETE TO audit_events DO INSTEAD NOTHING;
```

Audit is a **table, not a log stream**, because logs rotate and the answer to
"why did it post that?" must outlive log retention.

## 7. `review_cache`

```sql
CREATE TABLE review_cache (
    cache_key    TEXT PRIMARY KEY,   -- sha256(repo, pr, head_sha, config_hash)
    run_id       UUID NOT NULL REFERENCES runs(run_id),
    repo         TEXT NOT NULL,
    pr_number    INTEGER NOT NULL,
    head_sha     TEXT NOT NULL,
    payload      JSONB NOT NULL,     -- the rendered review
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`config_hash` covers prompt versions, model ids, and retrieval settings, so changing a
prompt invalidates the cache rather than silently serving a review the current code would
not produce.

## 8. `token_usage`

```sql
CREATE TABLE token_usage (
    usage_id      BIGSERIAL PRIMARY KEY,
    run_id        UUID NOT NULL,
    node          TEXT NOT NULL,        -- route|correctness|security|test_coverage|synthesise
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    latency_ms    INTEGER NOT NULL,
    finish_reason TEXT,
    usage_date    DATE NOT NULL,        -- daily budget rolls on this
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX token_usage_date_idx ON token_usage (usage_date);
```

Daily budget is `SUM(prompt_tokens + output_tokens) WHERE usage_date = today` — derived
from recorded fact, not a counter that can drift from reality.
