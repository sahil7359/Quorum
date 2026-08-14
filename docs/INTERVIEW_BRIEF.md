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
