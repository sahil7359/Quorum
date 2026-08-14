# Quorum as an MCP Server — published tool schema

Quorum both **consumes** MCP (the official GitHub MCP server, Phase 2) and **publishes** it
(this document, Phase 7). Publishing is the rarer half of the skill.

## The one thing to know

**This surface has no write path.** An MCP client calling Quorum gets findings, not side
effects. Publishing a finding to GitHub requires human approval through Quorum's own
interface and is not reachable from any tool here.

That is structural, not a policy. The server is constructed from three read-only callables
and holds a graph built with `approval=None, publish=None`, so the object it has **contains no
publish node**. There is nothing to call. If `review_pull_request` could post, any MCP client
could bypass the approval gate that the whole project exists to demonstrate.

Every response says so explicitly:

```json
{ "posted_to_github": false,
  "note": "Quorum's MCP surface is read-only. Publishing a finding requires human approval
           through Quorum's own interface and is not reachable from this server." }
```

## Connecting

```jsonc
// Claude Desktop / any MCP client config
{
  "mcpServers": {
    "quorum": {
      "command": "uv",
      "args": ["run", "python", "-m", "app.interface.mcp_entrypoint"],
      "cwd": "/path/to/quorum"
    }
  }
}
```

> The stdio entrypoint module is wired at the composition root in Phase 8. Until then the
> server is constructed directly — see `tests/support/quorum_mcp_stdio.py` for a runnable
> example.

Transport: **stdio** today. Streamable HTTP lands with the FastAPI service in Phase 8.

## Tools

### `review_pull_request`

Run a review and return grounded findings. **Never posts.**

| Input | Type | Notes |
| --- | --- | --- |
| `repo` | `string` | `owner/name`. Validated; `../` and multi-slash rejected |
| `pr_number` | `integer` | Must be positive |

Returns `run_id`, `repo`, `pr_number`, `status`, `specialists`, `routing_reason`, `findings`,
`dropped`, `diff_truncated`, `posted_to_github`, `note`.

Two fields are unusual and deliberate:

- **`routing_reason`** — *why* those specialists ran, not just which. Routing accuracy is a
  published metric; a client integrating against this deserves the rationale too.
- **`dropped`** — the cite-or-drop reasons for findings that did **not** survive grounding.
  Exposed rather than silently filtered, because "the model tried to cite something it wasn't
  shown" is information the caller may want.

Each finding carries a citation:

```json
{ "finding_id": "…", "specialist": "security", "severity": "high", "confidence": 0.9,
  "title": "Token never expires", "body": "…",
  "file_path": "app/auth/login.py", "line_start": 12,
  "citation": { "chunk_id": "5a5980876bf3a070",
                "file_path": "docs/security.md",
                "section_path": "Sessions > Expiry",
                "byte_range": [0, 200],
                "display": "docs/security.md — Sessions > Expiry" } }
```

A `Finding` cannot exist without a citation — it is a type-level guarantee, not a convention —
so the `citation` key is always present.

### `get_chunk`

Resolve a `chunk_id` to its text and locator.

| Input | Type |
| --- | --- |
| `chunk_id` | `string` — 16 lowercase hex chars; case is normalised |

**This tool exists so a client can verify a citation.** Without it, a chunk id is an opaque
token the caller has to trust; with it, grounding is checkable by whoever consumes the review
rather than only by someone with our database. That is the difference between "cite-or-drop"
being a claim and being an auditable property.

Returns `chunk_id`, `content`, `file_path`, `section_path`, `byte_range`, or
`{"error": "not_found"}`.

### `get_review`

Fetch a previously computed review by `run_id`. Returns the same shape as
`review_pull_request`, or `{"error": "not_found"}`.

### `list_ingested_repos`

Repositories whose documentation has been ingested and can be reviewed against. Reviews are
only meaningful for these — retrieval has nothing to ground against otherwise.

Returns `{"repos": [{"repo": "…", "ingested_sha": "…", "chunks": 164}]}`.

## Errors

Tools return `{"error": "..."}` in the payload rather than raising, so a client gets a
structured answer instead of a protocol-level failure. Malformed input (bad repo string,
non-positive PR number, malformed chunk id) is rejected before any work is done.

## What is tested

`tests/integration/test_quorum_mcp_server.py` drives this server with a **real MCP client over
real stdio** — the mirror of Phase 2, where a real client drove a fake GitHub server.

| Property | Test |
| --- | --- |
| Exactly the four documented tools are advertised | `test_server_advertises_exactly_the_documented_tools` |
| No write tool is present | `test_mcp_server_has_no_write_path` |
| Calling a write tool by name fails | `test_calling_a_write_tool_by_name_fails` |
| The module imports no code host at all | `test_the_server_module_never_imports_a_code_host` |
| Every response discloses that nothing was posted | `test_every_response_states_that_nothing_was_posted` |
| Every tool has a usable description | `test_every_tool_has_a_description` |
| A client can verify a citation | `test_a_client_can_verify_a_citation` |
| The response schema is stable | `test_review_record_serialisation_is_stable` |
