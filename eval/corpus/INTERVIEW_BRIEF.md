# Quorum — Interview Brief

Appended at the end of every phase. For each phase: five questions an interviewer would
plausibly ask, answered in my voice; the one thing most likely to be challenged with the
honest response; and every number produced, how it was measured, and what it does not prove.

Answers are first person, concise, no marketing language.

---

## Planning

### Five questions

**1. Why does this project exist when you already have DataChat?**

DataChat covers typed clean architecture, eval gates in CI, guardrails, and a deployed
zero-cost service. It doesn't cover three things: MCP — both consuming a third-party server
and publishing my own — multi-agent orchestration, and document RAG with real citations.
Quorum exists for exactly those three. Everything else in the repo is there to make them
credible, not to re-demonstrate what I've already shown.

**2. You call security a "baseline" here. Isn't that a cop-out?**

It would be if I skipped it. I didn't — there's a threat model, a 15-control guardrail table
mapped to tests, and an OWASP LLM plus Agentic Top 10 mapping. What I refused to do is turn
Quorum into a second security project, because DataChat already goes deep there: sqlglot AST
guardrail chain, read-only DB role, 30-case injection corpus fully blocked. Two projects
making the same argument is one wasted project. There *is* a security specialist agent in
Quorum, but that's a reviewer role — a product feature — not the same claim.

**3. Why three specialists and not one agent with a three-part prompt?**

Mainly because routing becomes measurable. A single agent decides which concerns to apply
invisibly inside one forward pass; a supervisor emits `{specialists, reason}` as data I can
log, replay, and score against a labelled set. Specialist routing accuracy is a published
metric and it only exists because the supervisor exists. Secondarily: cost — skipping a
specialist saves a call, and against a 100K token/day ceiling that's the difference between
two and four reviews — and context isolation, since each specialist gets only its own
retrieved chunks. The honest cost is that four calls beat one call on latency and tokens,
and I'd take the single prompt if routing weren't something I wanted to measure.

**4. Why not just let it post to GitHub automatically?**

Because "takes real action under human approval, with an audit log" is the claim the project
exists to make, and automatic posting deletes the claim. There's also a practical argument:
an ungrounded review bot that posts freely is a net negative for a maintainer — every false
positive costs a context switch. The gate is a durable LangGraph `interrupt()` backed by a
Postgres checkpointer, so a review proposed at 14:00 and approved at 19:00 resumes in a
different process. Free-tier instances sleep; an in-memory pause would lose the review.

**5. What's the hardest constraint and how did it shape the design?**

Groq's free tier: 100K tokens a day, 12K a minute. One review is 25–50K tokens, so 2–4 live
reviews a day, and the per-minute ceiling means specialists can't run in parallel without
tripping the limit. That shaped the design from Phase 0, not as an afterthought — cache by
commit SHA so gallery reviews compute once and serve forever, route specialists to an 8B
model and reserve the 70B for synthesis, cap diff size, and a global daily budget that falls
back to a cached review with an honest banner rather than degrading silently. Sequential
specialist dispatch is a consequence of a pricing tier, not an architectural belief, so it's
a config knob.

### Most likely to be challenged

> *"Cite-or-drop sounds strict, but it just means your reviewer stays quiet about anything
> the docs don't mention. Isn't that a system optimised to look good rather than be useful?"*

That's a fair hit and I'd concede most of it. Cite-or-drop does mean Quorum cannot report a
defect the repository's documentation doesn't speak to, so recall will never be high, and
the failure mode is silence. I chose it anyway for a specific reason: the failure mode I was
designing *against* is worse. An ungrounded reviewer that invents conventions costs a
maintainer a context switch per false positive, and that's how review bots get muted. I'd
rather ship something with a narrow, honest remit.

Where the challenge really lands is that a citation proves *grounding*, not *aptness* — a
finding can cite a real chunk that doesn't support it. I have no test for aptness. What I
have is retrieval eval bounding how often the wrong chunk comes back, and citations rendered
in the UI so a human can check in one click. That's a real gap and I'd say so rather than
claim the invariant is stronger than it is.

### Numbers produced in this phase

None. No code has been written and nothing has been measured. Every metric slot in
`docs/Tracker.md` and `README.md` currently reads `TODO: not yet measured`, which is the
correct state.

---

## Phase 0 — Scaffolding

### Five questions

**1. You have two mechanisms checking the same layer boundary. Isn't one enough?**

They fail for different reasons, which is the point. import-linter is configuration, and
configuration can be relaxed in the same commit that violates it — deleting a line from
`forbidden_modules` looks like tidying up. The AST test encodes the same rule as a test, so
weakening it is visible as weakening a test. There's also a concrete bug the pair caught:
import-linter needs `include_external_packages = true`, and without it "domain must not
import httpx" passes vacuously because httpx isn't a node in the graph. A green check that
isn't looking at anything is the exact failure mode I'm designing against.

**2. Why does the AST test parse the source instead of importing the module?**

Because importing a module to inspect its dependencies is circular. If `app/domain/foo.py`
imports `sqlalchemy`, then to detect that by importing `foo` you'd first have to
successfully import it — and if the dependency is missing in that environment, you get an
ImportError that looks like a broken test rather than a caught violation. Parsing detects
the violation without executing anything. I also used `sys.stdlib_module_names` rather than
a hand-maintained allowlist so the definition of "standard library" can't drift from the
interpreter the tests actually run on.

**3. You defined a cache key in Phase 0, before there was a cache. Why so early?**

Because the free-tier token budget is the binding constraint on the whole project — 100K
tokens a day, and one review is 25–50K. Caching by commit SHA isn't an optimisation I bolt
on later, it's what makes a six-PR gallery serveable at all. And the subtle part had to be
decided while I was thinking about it clearly: `config_hash` covers everything that changes
what a review *says* — prompt version, chunker version, models, retrieval settings, diff cap
— and deliberately excludes everything that only changes how we get there, like API keys and
base URLs. A cache that misses a prompt change is worse than no cache, because it's
confidently wrong; and one that invalidates when I rotate a key throws away the gallery for
no reason.

**4. Your architecture test bans `os` and `pathlib` in the application layer. That's
stricter than "no I/O" — why?**

The side effect is what I was actually after. If application code can't touch the
filesystem, prompts can't be loaded from disk, so they have to be constants in code. That's
guardrail G2 — the system prompt has no interpolation point — enforced structurally instead
of by code review. It's stricter than necessary and I know it. If Phase 4 makes it painful
I'll revisit it as a deliberate decision with an ADR, not by quietly adding an exception.

**5. How do you know your tests can fail?**

I broke each guarded thing and watched it go red before committing. Adding `import structlog`
to a domain module failed the AST test and broke the import-linter contract; adding
`import sqlalchemy` plus an infrastructure import to the application layer failed two tests
and broke two contracts; freezing `prompt_version` inside `config_hash()` failed the
parametrised cache-key test. Then I restored and the suite went green — 25 passed, mypy
clean, six contracts kept. I do this because my last project shipped a CI gate that asserted
a tautology and passed while scoring 0.0, and it sat in a public repo for weeks. Related: the
import-linter test *asserts* rather than skips when the binary is missing, because a silently
skipped architecture check is the same as no check.

### Most likely to be challenged

> *"Six import-linter contracts, an AST test suite, and four layers — for a project with
> three runtime dependencies and no features yet. Isn't this ceremony?"*

Partly, yes, and I'd concede the timing looks odd. The honest answer has two halves.

The half I'd defend: the boundary is cheap to establish now and expensive to retrofit. The
specific payoff is that eval runs against local Ollama and production runs against Groq as a
*config change*, which only works if nothing in application or domain knows which provider
exists. That property has to be true from the first adapter, not negotiated later.

The half I'd concede: for a project this size, a two-layer split — `core` and `adapters` —
would capture most of the benefit at half the ceremony, and I said so in ADR-0001 rather
than pretending four layers was obviously right. I went with four because separating *what
a finding is* from *how a review is orchestrated* is what keeps the domain stdlib-only, and
the stdlib-only property is what makes the AST fitness test possible at all. If the domain
and application layers merged, LangGraph would land in the pure layer and that test would
have nothing to check.

### Numbers produced in this phase

| Number | Value | How measured | What it does not prove |
| --- | --- | --- | --- |
| Tests passing | 25 | `uv run pytest` on the Phase 0 commit | Nothing about review quality — these test tooling and configuration, not behaviour |
| import-linter contracts kept | 6 of 6 | `uv run lint-imports` | That the contracts are *sufficient*, only that they hold. `include_external_packages` was needed to stop one passing vacuously |
| mypy `--strict` errors | 0 across 18 source files | `uv run mypy` | Very little yet — 18 files, most of them near-empty package inits |
| Runtime dependencies | 3 (`pydantic`, `pydantic-settings`, `structlog`) | `pyproject.toml` | Final count. Deps are added per phase, each with an ADR |

**Gate-failure proofs run this phase:** 3 of 3 (domain purity, application no-I/O, cache-key
sensitivity). Each broken deliberately, observed red, restored.

---

## Phase 1 — Domain core

### Five questions

**1. Why two finding types instead of one with an optional citation?**

Because with `citation: Citation | None`, "every surfaced finding is grounded" is a rule I
have to remember to enforce, at every call site, forever. With two types it's a transition
that can fail. `CandidateFinding` is what the model returned — untrusted, citation optional.
`Finding` has `citation: Citation`, not optional, so constructing one without a citation is a
TypeError. The only path between them is `ground_candidates()`, which drops what it can't
ground. The invariant holds even if every caller is wrong.

**2. `chunk_id` on the candidate is a bare `str | None`, but everything else in your domain
uses value objects. Isn't that inconsistent?**

It's deliberate. Constructing a `ChunkId` is already an assertion that the value is
well-formed hex of the right length — and at that point in the pipeline nobody has made that
assertion. It's raw model output that may be absent, malformed, or invented. Using the sloppy
type is the honest representation of what I actually have. It becomes a `ChunkId` at the
moment it's validated, which is inside the grounding function, and that's the point where the
type change means something.

**3. What stops the model citing a chunk id it saw somewhere else?**

That's the case I nearly missed. My first version checked "does this chunk id exist in the
corpus?", which catches a fabricated id but not a *real* chunk the specialist was never
shown. With three specialists retrieving over the same corpus, ids can leak between contexts,
and an existence check waves those through. So grounding takes visibility per specialist —
`visible: Mapping[SpecialistKind, Sequence[ChunkId]]` — and checks membership in what *that*
specialist actually received. It's one of four drop reasons, and I kept the drops rather than
filtering silently, because "how often does the model try to cite something it should not" is
a number I want in Phase 6.

**4. You changed a design document during this phase. What did you get wrong?**

`Schema.md` said a chunk id "resolves back to a full (file, section, offset) locator". Writing
the code made me realise that's false — the id is `sha256(locator)[:16]`, and you can't
recover a locator from a hash. What's actually true is the reverse: the locator is stored in
columns beside the id and the pair can be *verified*. I reworded the doc and renamed the test
to `test_chunk_id_verifies_against_its_locator`. It's a small thing, but "round-trips" is a
claim I couldn't have demonstrated if someone asked me to, and those are the claims that cost
you.

**5. Why does `Severity` have a `rank` property instead of just being sorted?**

Because it's a `StrEnum` so it serialises straight to Postgres, and sorted as strings you get
`high < info < low < medium` — exactly backwards. It fails quietly: the review renders fine,
it just buries the worst finding at the bottom. So all ranking goes through
`rank_key = (severity.rank, confidence)`, and there's a test asserting alphabetical order is
*not* severity order, purely so the next person reaching for the default comparison finds out
immediately instead of shipping it.

### Most likely to be challenged

> *"You've written 133 tests and a domain layer with eleven ports, and the system can't
> review anything yet. Isn't the type-level ceremony doing work that a couple of assertions
> in the synthesis function would do just as well?"*

For most of it, the honest answer is that a couple of assertions would be fine. `RepoRef`
validation, `TokenUsage` non-negativity, the `PullRequest` number check — those are cheap
either way and I wouldn't defend the value-object version as obviously superior.

Where I'd push back is the `CandidateFinding`/`Finding` split specifically, and I'd make the
argument concretely rather than on principle. Cite-or-drop is checked in exactly one place
today, in synthesis. Phase 7 adds an MCP server that returns findings, and Phase 8 adds an
SSE stream that emits them as they form — two more paths where findings reach a consumer. An
assertion in synthesis protects one of those three. A type that cannot be constructed
uncited protects all three, including the ones I haven't written yet. That's the trade: more
ceremony now, and a class of bug that can't be introduced later by someone adding a call
site.

The part I'd concede without argument: eleven ports for a system with roughly two real
adapters so far does look like anticipatory design, and some of them will turn out to have
exactly one implementation plus a fake. I'd rather explain that than discover at Phase 8 that
the retrieval interface assumed something the pgvector adapter can't do.

### Numbers produced in this phase

| Number | Value | How measured | What it does not prove |
| --- | --- | --- | --- |
| Tests passing | 133 | `uv run pytest` on the Phase 1 commit | Nothing about review quality. These are domain invariants — no model, no retrieval, no I/O has run |
| mypy `--strict` errors | 0 across 27 files | `uv run mypy` | — |
| import-linter contracts kept | 6 of 6 | `uv run lint-imports` | — |
| Domain runtime dependencies | 0 | `app/domain` imports only stdlib, asserted by AST test | — |
| Protocol ports defined | 11 | `tests/architecture/test_ports_are_protocols.py` | That the interfaces are *right* — most have no real adapter yet |

**Gate-failure proofs run this phase:** 3 of 3 — file-level chunk ids (4 tests red),
removing the per-specialist visibility check (2 tests red), giving a port an implementation
(1 test red). All restored.

---

## Phase 2 — MCP client

### Five questions

**1. You had no GitHub token. How did you test an integration?**

I built a fake GitHub MCP server using the real MCP SDK and had the client talk to it over
real stdio — a genuine subprocess, handshake, tool discovery and structured results. Only
GitHub itself is fake. That means the things I could plausibly get wrong — protocol handling,
unwrapping `structured_content`, propagating `is_error`, connection lifecycle — are actually
exercised, whereas a mocked session would have tested my mock. What I can't claim is that the
argument names match the real server; those come from its documented schema, not a live
handshake, and that's the first thing to verify when a token exists.

**2. Why are the allowlist and write guard in the client rather than in the graph?**

Because a guard in the `publish` node is a property of one call path, and a guard in the
client is a property of the client. Today `publish` is the only caller — but Phase 7 adds an
MCP server exposing Quorum's review capability and Phase 8 adds an HTTP API. Neither would
inherit a check that lives in a graph node. The claim I want to make is "this agent cannot
merge your pull request", and that has to be true of the thing holding the credential. There's
a test that reaches past the public method straight into `_call` and still gets refused.

**3. Isn't the `public_repo` token scope enough on its own?**

It's a real control and it stays, but it bounds the damage *class*, not the *target* —
`public_repo` still permits commenting on any public repository. It also fails the
demonstration test: "GitHub would have stopped it" isn't something a reviewer of my codebase
can see. The allowlist makes the write surface two named tools, asserted by a test, so if it
ever changes that shows up in review rather than in a log.

**4. Tell me about a bug you found in this phase.**

The good one came from the rule that every gate has to be proven able to fail. The diff
parser excluded lines starting with `+++` on the grounds that they're file headers. I removed
the exclusion expecting a test to go red — and the suite stayed green. Two problems. First,
the guard was dead code: the parser only counts inside a hunk, and `+++ b/path` headers sit
before the first `@@`, so they can never reach the counter. Second, it was actively wrong —
an added line whose *content* starts with `++` arrives as `+++...` and was being silently
uncounted. That's not hypothetical here, because my retrieval corpus is documentation and a
CONTRIBUTING.md explaining how to read a patch contains exactly those lines. I deleted the
guard rather than fixing it and wrote two tests that exercise the real case. The lesson I
took: I had a rationale comment, a matching test name and a passing suite — three signals
agreeing, all wrong.

**5. What surprised you about the MCP SDK?**

It had moved. `mcp` resolved to 2.0.0, where `FastMCP` no longer exists — it's `MCPServer` —
and result fields are snake_case rather than the camelCase of v1. I found that by writing a
twenty-line smoke script and running it before writing any real code, which cost ten minutes
and saved an hour debugging a client written against an API that no longer existed. It's a
habit I'd keep: verify the shape of a dependency's API by running it, not by recalling it.

### Most likely to be challenged

> *"Your integration test talks to a server you wrote. You've verified that your client can
> talk to your own fake — which is close to testing nothing. The real GitHub MCP server will
> behave differently and you have no idea how."*

Largely fair, and I'd separate what the test does and doesn't establish rather than defend
it wholesale.

What it genuinely establishes: the MCP protocol layer works. The subprocess launches, the
JSON-RPC handshake completes, tools are discovered, structured results unwrap correctly,
error results become domain errors, and the connection tears down cleanly. None of that is
my fake's behaviour — it's the SDK's, and it's the layer where I'd otherwise have made
mistakes. The guards are also genuinely tested: `test_destructive_tool_is_refused_even_when_advertised`
refuses a `delete_repository` tool that really is advertised by the connected server.

What it does not establish, and I'd say so unprompted: that GitHub's actual argument names
and response shapes match my fixtures. `pullNumber` vs `pull_number`, the exact nesting of
`head.sha` — those come from documentation, and documentation drifts. If they're wrong, every
read path fails on first contact.

The honest summary is that I've tested the half I control and fixtured the half I can't
reach, and I know which is which. Given the alternative was mocking the session entirely, I'd
make the same call again — but the first thing I'd do with a token is run the read paths
against the real server and fix what the fixtures got wrong.

### Numbers produced in this phase

| Number | Value | How measured | What it does not prove |
| --- | --- | --- | --- |
| Tests passing | 196 (22 over real stdio) | `uv run pytest` | Nothing about the *real* GitHub MCP server — argument names and payload shapes are fixtured from documentation |
| Write surface | 2 tools | `test_write_surface_is_exactly_two_tools` | That the token cannot do more; it bounds what *Quorum* reaches for |
| mypy `--strict` errors | 0 across 36 files | `uv run mypy` | — |
| Bugs found by gate proofs | 1 | Removing the `+++` exclusion left the suite green | — |

**Gate-failure proofs run this phase:** 3 attempted, **1 initially failed to fail** — which
found the diff-parser bug. After the fix, all 3 confirmed: allowlist removed (2 red), write
guard removed (4 red), wrong `+++` exclusion reinstated (2 red).
