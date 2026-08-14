"""One real review, end to end, against local Ollama and the real retriever.

This is a **smoke run, not an evaluation**. It proves the stack holds together with a real
model — routing parses, specialists return schema-valid JSON, citations resolve, grounding
runs — on exactly one hand-written diff. It says nothing about review quality, and no number
it prints should ever be quoted as a metric.

The real measurement is Phase 6, against merged pull requests carrying human review comments.

Run: ``uv run python -m eval.smoke.live_review``
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from app.application.agents.graph import build_review_graph
from app.application.agents.nodes import IngestNode, RouteNode, SpecialistsNode, SynthesiseNode
from app.domain.entities import ChangedFile, Diff, DiffHunk, PullRequest, RepoRef
from app.domain.values import RunId
from app.infrastructure.llm.ollama import OllamaChatModel
from app.infrastructure.retrieval.chunker import chunk_markdown
from app.infrastructure.retrieval.dense import FastEmbedEmbedder, InMemoryChunkStore
from app.infrastructure.retrieval.hybrid import HybridRetriever
from tests.support.fakes import FakeCodeHost, NullTracer, RecordingLogger

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS = REPO_ROOT / "eval" / "corpus"
REPO = RepoRef.parse("sahil/quorum")
COMMIT = "smoke"

# A diff that plainly violates a rule the corpus states: docs/Rules.md requires a `# why:`
# comment naming the rejected alternative, and docs/Guardrails.md requires diff content to be
# fenced. This one adds an unfenced prompt interpolation with no rationale.
SOURCE = '''"""Prompt building."""


def build_prompt(system: str, diff: str) -> str:
    return system + "\\n" + diff


def summarise(text: str) -> str:
    return text[:100]
'''

DIFF_BODY = "+def build_prompt(system: str, diff: str) -> str:\n+    return system + diff"


async def main() -> None:
    logger = RecordingLogger()
    tracer = NullTracer()

    print("building corpus…")
    chunks = []
    for path in sorted(CORPUS.rglob("*.md")):
        chunks.extend(
            chunk_markdown(
                path.read_text(encoding="utf-8"),
                repo=str(REPO),
                commit_sha=COMMIT,
                file_path="docs/" + path.relative_to(CORPUS).as_posix(),
            )
        )
    embedder = FastEmbedEmbedder()
    store = InMemoryChunkStore()
    await store.upsert(chunks, await embedder.embed([c.content for c in chunks]))
    print(f"  {len(chunks)} chunks indexed")

    retriever = HybridRetriever(store=store, embedder=embedder, logger=logger, candidates=30)
    model = OllamaChatModel(base_url="http://localhost:11434", model="llama3.1:8b", logger=logger)

    hunk = DiffHunk("app/application/agents/prompts.py", 1, 8, 1, 8, DIFF_BODY)
    host = FakeCodeHost(
        pull_request=PullRequest(
            repo=REPO,
            number=1,
            title="Add prompt builder",
            body="Builds the specialist prompt.",
            author="sahil",
            base_sha="base",
            head_sha=COMMIT,
        ),
        diff=Diff(
            files=(ChangedFile("app/application/agents/prompts.py", "modified", 2, 0, (hunk,)),),
            raw="diff --git a/app/application/agents/prompts.py b/app/application/agents/prompts.py\n",
        ),
        files={"app/application/agents/prompts.py": SOURCE},
    )

    graph = build_review_graph(
        ingest=IngestNode(code_host=host, logger=logger, tracer=tracer, max_diff_lines=1500),
        route=RouteNode(model=model, logger=logger, tracer=tracer),
        specialists=SpecialistsNode(
            retriever=retriever, model=model, logger=logger, tracer=tracer, top_k=5
        ),
        synthesise=SynthesiseNode(logger=logger, tracer=tracer),
    )

    print("running review…")
    started = time.perf_counter()
    result = await graph.ainvoke(
        {"run_id": RunId.new(), "repo": REPO, "pr_number": 1, "commit_sha": COMMIT}
    )
    elapsed = time.perf_counter() - started

    routing = result["routing"]
    print(f"\n--- routing ---\n  specialists: {[k.value for k in routing.specialists]}")
    print(f"  reason     : {routing.reason}")
    print(f"  llm added  : {[k.value for k in routing.llm_added]}")

    usage = result.get("usage", [])
    total = sum(u.total_tokens for u in usage)
    print(f"\n--- cost ---\n  {len(usage)} model calls, {total} tokens, {elapsed:.1f}s wall clock")

    findings = result["findings"]
    dropped = result["dropped"]
    print(f"\n--- findings ---\n  surfaced: {len(findings)}   dropped: {len(dropped)} {dropped}")
    for finding in findings:
        print(f"\n  [{finding.severity.value}] {finding.specialist.value}: {finding.title}")
        print(f"    {finding.body[:160]}")
        print(f"    cites {finding.citation.chunk_id} -> {finding.citation.display}")

    print("\nNOTE: this is a smoke run on one hand-written diff. Not an evaluation.")


if __name__ == "__main__":
    asyncio.run(main())
