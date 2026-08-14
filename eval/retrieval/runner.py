"""Retrieval eval runner.

Ingests this repository's own ``docs/`` tree, then scores four retrieval configurations
against the golden set:

* ``dense``       — dense only
* ``bm25``        — sparse only
* ``hybrid``      — dense + BM25 fused by RRF
* ``hybrid+rerank`` — the above, then a cross-encoder

The headline output is the **rerank delta**. If reranking does not earn its latency it gets
cut, and that is a finding rather than a failure.

Run: ``uv run python -m eval.retrieval.runner``
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from app.domain.entities import Chunk, RepoRef
from app.infrastructure.retrieval.chunker import chunk_markdown
from app.infrastructure.retrieval.dense import FastEmbedEmbedder, InMemoryChunkStore
from app.infrastructure.retrieval.fusion import reciprocal_rank_fusion
from app.infrastructure.retrieval.hybrid import FastEmbedReranker
from app.infrastructure.retrieval.sparse import BM25Index
from eval.retrieval.goldenset import GOLDEN_QUERIES, relevant_chunk_ids
from eval.retrieval.metrics import mean, ndcg_at_k, recall_at_k, success_at_k

REPO_ROOT = Path(__file__).resolve().parents[2]
LIVE_DOCS = REPO_ROOT / "docs"
CORPUS_DIR = REPO_ROOT / "eval" / "corpus"
"""The eval corpus is a **frozen snapshot**, not the live docs/ tree.

Pointing it at docs/ directly created a feedback loop I hit in this very phase: the ADR
recording the reranking result is itself a corpus document, so writing it changed the
numbers it was recording, which made the ADR stale, which meant editing it, which changed
the numbers again. A frozen snapshot converges. Refresh deliberately with --snapshot.
"""
REPO = RepoRef.parse("sahil/quorum")
COMMIT = "eval-corpus-v1"
K = 5
CANDIDATES = 30


@dataclass
class ConfigResult:
    config: str
    ndcg_at_5: float
    recall_at_5: float
    success_at_5: float
    queries_scored: int
    latency_ms_per_query: float


@dataclass
class EvalReport:
    corpus_sha: str
    corpus_files: int
    corpus_chunks: int
    queries: int
    k: int
    embedding_model: str
    rerank_model: str
    results: list[ConfigResult]
    rerank_delta_ndcg: float
    rerank_delta_recall: float


def _sorted_corpus_files() -> list[Path]:
    """Every corpus markdown file, in a deterministic, platform-independent order.

    why: not ``sorted(CORPUS_DIR.rglob(...))``. ``pathlib.PureWindowsPath`` compares
    case-*insensitively* (Windows filesystems are case-insensitive); Linux's is
    case-*sensitive*. ``adr/0001-....md`` sorts before ``AppFlow.md`` on Windows and after it
    on Linux -- 'a' and 'A' compare equal on Windows so the tie breaks on the second
    character, but compare unequal on Linux so the first character alone decides it. Found
    live: identical committed content, byte-identical after the read_text() fix above, still
    produced two different fingerprints, because :func:`corpus_fingerprint` hashes
    incrementally and file *order* changes the hash even when no file's content does. Sorting
    on the explicit relative-path string, which ``str.__lt__`` always compares
    case-sensitively regardless of OS, makes the order the same everywhere.
    """
    return sorted(CORPUS_DIR.rglob("*.md"), key=lambda p: p.relative_to(CORPUS_DIR).as_posix())


def corpus_fingerprint() -> str:
    """Hash of every corpus file's content.

    The eval corpus is this repository's own docs/ tree, so *writing documentation
    changes the retrieval numbers*. Without a fingerprint the gate reports that as a
    regression, which is both alarming and wrong -- the retriever did not change, the
    corpus did. Recording it lets the gate say 'incomparable' instead of 'regressed'.

    why: hashes ``read_text()``, not ``read_bytes()``. Found live -- CI (Linux, LF) and a
    Windows checkout of the identical committed content produced different fingerprints,
    because raw bytes preserve whatever line ending the checkout happened to produce, and
    ``.gitattributes``' ``eol=lf`` does not retroactively rewrite a working tree that predates
    it. ``read_text()`` does Python's universal-newline translation (``\\r\\n``/``\\r`` -> ``\\n``)
    on the way in, which is also what :func:`load_corpus` already reads with -- the fingerprint
    now hashes the same normalised content the chunker actually sees, on every platform.
    alt: fix it at the git-checkout layer (renormalise, force autocrlf) -- more fragile, and
    still leaves the fingerprint computing something different from what gets chunked
    """
    digest = hashlib.sha256()
    for path in _sorted_corpus_files():
        digest.update(path.relative_to(CORPUS_DIR).as_posix().encode())
        digest.update(path.read_text(encoding="utf-8").encode("utf-8"))
    return digest.hexdigest()[:16]


def load_corpus() -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in _sorted_corpus_files():
        relative = "docs/" + path.relative_to(CORPUS_DIR).as_posix()
        chunks.extend(
            chunk_markdown(
                path.read_text(encoding="utf-8"),
                repo=str(REPO),
                commit_sha=COMMIT,
                file_path=relative,
            )
        )
    return chunks


async def run(*, verbose: bool = True) -> EvalReport:
    chunks = load_corpus()
    if not chunks:
        raise SystemExit(f"no markdown found under {CORPUS_DIR}")

    embedder = FastEmbedEmbedder()
    store = InMemoryChunkStore()
    vectors = await embedder.embed([c.content for c in chunks])
    await store.upsert(chunks, vectors)

    bm25 = BM25Index.build([(str(c.chunk_id), c.content) for c in chunks])
    reranker = FastEmbedReranker()
    by_id = {str(c.chunk_id): c for c in chunks}

    scores: dict[str, dict[str, list[float]]] = {
        name: {"ndcg": [], "recall": [], "success": []}
        for name in ("dense", "bm25", "hybrid", "hybrid+rerank")
    }
    elapsed: dict[str, float] = dict.fromkeys(scores, 0.0)
    skipped: list[str] = []

    for golden in GOLDEN_QUERIES:
        relevant = relevant_chunk_ids(golden, list(chunks))
        if not relevant:
            # A label that matches nothing would silently score 0 and drag the mean down,
            # which looks like bad retrieval instead of a bad label. Skip loudly instead.
            skipped.append(golden.query_id)
            continue

        query_vector = (await embedder.embed([golden.query]))[0]

        start = time.perf_counter()
        dense_hits = await store.search_dense(
            query_vector, repo=REPO, commit_sha=COMMIT, limit=CANDIDATES
        )
        dense_ids = [str(h.chunk_id) for h in dense_hits]
        dense_seconds = time.perf_counter() - start
        elapsed["dense"] += dense_seconds

        start = time.perf_counter()
        sparse_ids = [doc_id for doc_id, _ in bm25.search(golden.query, limit=CANDIDATES)]
        sparse_seconds = time.perf_counter() - start
        elapsed["bm25"] += sparse_seconds

        start = time.perf_counter()
        fused = [doc_id for doc_id, _ in reciprocal_rank_fusion([dense_ids, sparse_ids])]
        fusion_seconds = time.perf_counter() - start

        start = time.perf_counter()
        candidates = fused[:CANDIDATES]
        rerank_scores = list(
            reranker._loaded().rerank(golden.query, [by_id[i].content for i in candidates])
        )
        reranked = [
            doc_id
            for doc_id, _ in sorted(
                zip(candidates, rerank_scores, strict=True), key=lambda p: -float(p[1])
            )
        ]
        rerank_seconds = time.perf_counter() - start

        # why: latency must be the cost of *serving that configuration*, not the cost of its
        #      last step. Timing only the fusion made "hybrid" look like 0.03 ms/query, which
        #      is true of the fusion and a lie about the configuration -- it still has to run
        #      both retrievers first. Exactly the kind of plausible number I must not publish.
        #      alt: time each stage in isolation (accurate per stage, misleading per config)
        elapsed["hybrid"] += dense_seconds + sparse_seconds + fusion_seconds
        elapsed["hybrid+rerank"] += dense_seconds + sparse_seconds + fusion_seconds + rerank_seconds

        for name, ranking in (
            ("dense", dense_ids),
            ("bm25", sparse_ids),
            ("hybrid", fused),
            ("hybrid+rerank", reranked),
        ):
            scores[name]["ndcg"].append(ndcg_at_k(ranking, relevant, K))
            scores[name]["recall"].append(recall_at_k(ranking, relevant, K))
            scores[name]["success"].append(success_at_k(ranking, relevant, K))

    scored_count = len(GOLDEN_QUERIES) - len(skipped)
    results = [
        ConfigResult(
            config=name,
            ndcg_at_5=round(mean(values["ndcg"]), 4),
            recall_at_5=round(mean(values["recall"]), 4),
            success_at_5=round(mean(values["success"]), 4),
            queries_scored=scored_count,
            latency_ms_per_query=round(elapsed[name] * 1000 / max(scored_count, 1), 2),
        )
        for name, values in scores.items()
    ]
    by_config = {r.config: r for r in results}

    report = EvalReport(
        corpus_sha=corpus_fingerprint(),
        corpus_files=len({c.locator.file_path for c in chunks}),
        corpus_chunks=len(chunks),
        queries=scored_count,
        k=K,
        embedding_model="BAAI/bge-small-en-v1.5",
        rerank_model="Xenova/ms-marco-MiniLM-L-6-v2",
        results=results,
        rerank_delta_ndcg=round(
            by_config["hybrid+rerank"].ndcg_at_5 - by_config["hybrid"].ndcg_at_5, 4
        ),
        rerank_delta_recall=round(
            by_config["hybrid+rerank"].recall_at_5 - by_config["hybrid"].recall_at_5, 4
        ),
    )

    if verbose:
        print(
            f"corpus: {report.corpus_files} files, {report.corpus_chunks} chunks, sha {report.corpus_sha}"
        )
        print(f"queries scored: {report.queries}/{len(GOLDEN_QUERIES)}")
        if skipped:
            print(f"SKIPPED (labels matched no chunk): {skipped}")
        print(f"\n{'config':<16}{'NDCG@5':>9}{'Recall@5':>10}{'Success@5':>11}{'ms/query':>10}")
        for r in results:
            print(
                f"{r.config:<16}{r.ndcg_at_5:>9.4f}{r.recall_at_5:>10.4f}"
                f"{r.success_at_5:>11.4f}{r.latency_ms_per_query:>10.2f}"
            )
        print(f"\nrerank delta NDCG@5:   {report.rerank_delta_ndcg:+.4f}")
        print(f"rerank delta Recall@5: {report.rerank_delta_recall:+.4f}")

    return report


def snapshot_corpus() -> int:
    """Refresh the frozen corpus from the live docs/ tree. A deliberate act."""
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    for stale in CORPUS_DIR.rglob("*.md"):
        stale.unlink()
    copied = 0
    for source in sorted(LIVE_DOCS.rglob("*.md")):
        target = CORPUS_DIR / source.relative_to(LIVE_DOCS)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        copied += 1
    return copied


def main() -> None:
    parser = argparse.ArgumentParser(description="Quorum retrieval eval")
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--snapshot", action="store_true", help="refresh eval/corpus from docs/")
    args = parser.parse_args()

    if args.snapshot:
        print(f"snapshotted {snapshot_corpus()} files into {CORPUS_DIR.relative_to(REPO_ROOT)}")

    report = asyncio.run(run())

    runs_dir = REPO_ROOT / "eval" / "_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "retrieval-latest.json").write_text(
        json.dumps(asdict(report), indent=2), encoding="utf-8"
    )

    if args.write_baseline:
        baseline = REPO_ROOT / "eval" / "baselines" / "retrieval.json"
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")
        print(f"\nbaseline written to {baseline.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
