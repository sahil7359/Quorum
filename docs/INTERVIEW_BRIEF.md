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
