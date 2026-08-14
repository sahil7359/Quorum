# ADR-0003 — Tool allowlist and write authorisation live in the MCP adapter

- **Status:** Accepted
- **Date:** 2026-08-14
- **Phase:** 2

## Context

Quorum consumes a third-party MCP server it does not control, using a credential that can
write to GitHub. Two questions need answers a stranger can verify:

1. Which of the server's tools may Quorum call at all?
2. What must be true before a tool that *changes something* is reachable?

The natural place to put both answers is the graph — the `publish` node is the only node
that should write, so checking there is obvious and reads well.

That placement is wrong, and the reason is worth writing down rather than discovering later.

## Decision

**Both guards live in `GitHubMcpClient`, not in the graph.**

```python
async def _call(self, tool, arguments, *, write_authorised: bool = False):
    if not is_allowed(tool):
        raise ToolNotAllowedError(tool)                    # G3
    if is_write(tool) and not write_authorised:
        raise ApprovalRequiredError(...)                   # G4
```

`write_authorised=True` is passed by exactly two methods, and only after
`approval.authorises(finding)` — which checks both the finding identity *and* the
`payload_hash`, so text edited after approval loses its authorisation.

Supporting decisions:

- **Allowed and write are separate predicates.** A tool being on the allowlist does not make
  it writable. Enforced by `test_read_and_write_sets_are_disjoint`.
- **At connect time**, a missing required *read* tool is fatal, and unexpected advertised
  tools are logged rather than ignored.
- **The write surface is asserted as a test**, not described in prose:
  `test_write_surface_is_exactly_two_tools`.

## Alternatives considered

**Guards in the `publish` graph node.** Simplest, reads naturally, and the node is the only
caller *today*. Rejected because it makes the guarantee a property of one call path rather
than of the client. Phase 7 adds an MCP server exposing Quorum's review capability and Phase
8 adds an HTTP API — two more callers, neither of which would inherit a graph-node check.
The claim I want to be able to make is "this agent cannot merge your pull request", and that
has to be true of the thing holding the credential.

**Rely on token scoping alone.** The PAT is `public_repo`-scoped, so GitHub itself refuses
most damage. Genuinely a real control, and it stays. Rejected as *sufficient* because
`public_repo` still permits commenting on any public repository — the scope bounds the
damage class, not the target. It also fails the demonstration test: "GitHub would have
stopped it" is not something a reviewer of this codebase can see.

**A capability object passed to nodes** — give `publish` a `WritableCodeHost` and everything
else a `ReadOnlyCodeHost`, so an unauthorised write is a type error. Genuinely the strongest
option, and I nearly took it. Rejected because it needs two Protocols, two adapters wrapping
one client, and wiring that distinguishes them at the composition root — real complexity for
a system with one write path. The current design gets most of the benefit by requiring
`approval: Approval` as an argument on the write methods, which already makes an
unauthorised write hard to express by accident. If a second write path ever appears, this is
the first thing to revisit.

**Trusting the MCP server's own permissions.** Rejected outright: the server does whatever
the token allows, and the token is ours.

## Consequences

**Good**

- The guarantee holds for every caller, including the two that do not exist yet.
- The blast radius is a test, so a change to it is visible in review.
- A server that gains a destructive tool is logged, and refused if anything reaches for it.

**Bad, and accepted**

- The check is duplicated in spirit: the graph also routes to `publish` only after approval,
  so there are two mechanisms saying the same thing. That is deliberate belt-and-braces, but
  it does mean two places to keep consistent.
- `_call` is a private method that tests reach into directly, which is not lovely. I decided
  a test proving the transport itself refuses an unauthorised write is worth more than
  keeping the test suite to the public surface.
- The allowlist is a hardcoded frozenset. Adding a legitimate tool needs a code change and a
  test update. That friction is the point, but it *is* friction.

## Invariant and test

> **Invariant:** no tool outside `ALLOWED_TOOLS` is ever invoked, and no tool in
> `WRITE_TOOLS` is invoked without an `Approval` matching the exact finding and payload hash.

Enforced by `tests/integration/test_github_mcp_client.py::TestAllowlist` and `::TestWriteGuard`,
and by `tests/security/test_mcp_allowlist.py`. Proven to fail by removing each guard in turn
— 2 and 4 tests red respectively — recorded in `HANDOFF.md`, Phase 2.
