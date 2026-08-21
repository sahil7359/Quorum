"""The review cache. What makes a gallery of cached reviews free to serve forever.

Keyed by ``cache_key = sha256(repo, pr, head_sha, config_hash)`` (``Settings.config_hash()``,
Phase 0) so that changing a prompt, model id, or retrieval setting invalidates the cache rather
than silently serving a review the current code would not produce. See ``docs/AppFlow.md`` §2.

SQLite, same scope decision as ``persistence/audit.py``: durability without a service
dependency in local dev or CI. The Postgres adapter is a port implementation away; it is
explicitly the *next* piece of deployment work after this one, not part of it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.domain.entities import Citation, Finding, RepoRef, Review, RoutingDecision
from app.domain.values import (
    ChunkId,
    ChunkLocator,
    FindingId,
    RunId,
    RunStatus,
    Severity,
    SpecialistKind,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_cache (
    cache_key    TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    repo         TEXT NOT NULL,
    pr_number    INTEGER NOT NULL,
    head_sha     TEXT NOT NULL,
    payload      TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS review_cache_repo_pr_idx
    ON review_cache (repo, pr_number, created_at);
"""


class SqliteReviewCache:
    """Adapter satisfying ``ReviewCachePort``."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(SCHEMA)
        self._connection.commit()

    async def get(self, cache_key: str) -> Review | None:
        row = self._connection.execute(
            "SELECT payload FROM review_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        if row is None:
            return None
        return _review_from_json(json.loads(row["payload"]))

    async def get_latest(self, repo: RepoRef, pr_number: int) -> Review | None:
        # why: ORDER BY created_at alone can tie when two puts land in the same microsecond
        # (plausible on a fast local run); rowid DESC as a tiebreaker makes "most recent"
        # deterministic rather than "whichever the query planner happened to return".
        row = self._connection.execute(
            "SELECT payload FROM review_cache WHERE repo = ? AND pr_number = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (str(repo), pr_number),
        ).fetchone()
        if row is None:
            return None
        return _review_from_json(json.loads(row["payload"]))

    async def list_recent(self, limit: int) -> list[Review]:
        rows = self._connection.execute(
            "SELECT payload FROM review_cache ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_review_from_json(json.loads(row["payload"])) for row in rows]

    async def put(self, cache_key: str, review: Review) -> None:
        # why: INSERT OR REPLACE rather than INSERT -- a config change that invalidates a key
        #      (a new prompt version, say) produces a *different* cache_key naturally, so a
        #      collision on the same key means the same repo/pr/sha/config was reviewed twice
        #      and the newer result is the one worth keeping, not an error to guard against.
        self._connection.execute(
            "INSERT OR REPLACE INTO review_cache "
            "(cache_key, run_id, repo, pr_number, head_sha, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                cache_key,
                str(review.run_id),
                str(review.repo),
                review.pr_number,
                review.head_sha,
                json.dumps(_review_to_json(review)),
                datetime.now(UTC).isoformat(),
            ),
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()


def _review_to_json(review: Review) -> dict[str, object]:
    return {
        "run_id": str(review.run_id),
        "repo": str(review.repo),
        "pr_number": review.pr_number,
        "head_sha": review.head_sha,
        "status": review.status.value,
        "routing": {
            "specialists": [k.value for k in review.routing.specialists],
            "reason": review.routing.reason,
            "heuristic_floor": [k.value for k in review.routing.heuristic_floor],
            "llm_added": [k.value for k in review.routing.llm_added],
            "llm_removal_ignored": [k.value for k in review.routing.llm_removal_ignored],
        },
        "findings": [_finding_to_json(f) for f in review.findings],
        "diff_truncated": review.diff_truncated,
        "started_at": review.started_at.isoformat() if review.started_at else None,
        "finished_at": review.finished_at.isoformat() if review.finished_at else None,
        "error": review.error,
    }


def _finding_to_json(finding: Finding) -> dict[str, object]:
    locator = finding.citation.locator
    return {
        "finding_id": str(finding.finding_id),
        "specialist": finding.specialist.value,
        "severity": finding.severity.value,
        "confidence": finding.confidence,
        "title": finding.title,
        "body": finding.body,
        "citation": {
            "chunk_id": str(finding.citation.chunk_id),
            "quote": finding.citation.quote,
            "locator": {
                "repo": locator.repo,
                "commit_sha": locator.commit_sha,
                "file_path": locator.file_path,
                "section_path": locator.section_path,
                "start_offset": locator.start_offset,
                "end_offset": locator.end_offset,
            },
        },
        "file_path": finding.file_path,
        "line_start": finding.line_start,
        "line_end": finding.line_end,
    }


def _review_from_json(data: dict[str, Any]) -> Review:
    routing_data = data["routing"]
    routing = RoutingDecision(
        specialists=tuple(SpecialistKind(v) for v in routing_data["specialists"]),
        reason=str(routing_data["reason"]),
        heuristic_floor=tuple(SpecialistKind(v) for v in routing_data["heuristic_floor"]),
        llm_added=tuple(SpecialistKind(v) for v in routing_data["llm_added"]),
        llm_removal_ignored=tuple(SpecialistKind(v) for v in routing_data["llm_removal_ignored"]),
    )
    return Review(
        run_id=RunId(str(data["run_id"])),
        repo=RepoRef.parse(str(data["repo"])),
        pr_number=int(data["pr_number"]),
        head_sha=str(data["head_sha"]),
        status=RunStatus(data["status"]),
        routing=routing,
        findings=tuple(_finding_from_json(f) for f in data["findings"]),
        diff_truncated=bool(data["diff_truncated"]),
        started_at=datetime.fromisoformat(data["started_at"]) if data["started_at"] else None,
        finished_at=datetime.fromisoformat(data["finished_at"]) if data["finished_at"] else None,
        error=data["error"] if isinstance(data["error"], str) else None,
    )


def _finding_from_json(data: dict[str, Any]) -> Finding:
    citation_data = data["citation"]
    locator_data = citation_data["locator"]
    locator = ChunkLocator(
        repo=str(locator_data["repo"]),
        commit_sha=str(locator_data["commit_sha"]),
        file_path=str(locator_data["file_path"]),
        section_path=str(locator_data["section_path"]),
        start_offset=int(locator_data["start_offset"]),
        end_offset=int(locator_data["end_offset"]),
    )
    citation = Citation(
        chunk_id=ChunkId(str(citation_data["chunk_id"])),
        locator=locator,
        quote=str(citation_data.get("quote", "")),
    )
    return Finding(
        finding_id=FindingId(str(data["finding_id"])),
        specialist=SpecialistKind(data["specialist"]),
        severity=Severity(data["severity"]),
        confidence=float(data["confidence"]),
        title=str(data["title"]),
        body=str(data["body"]),
        citation=citation,
        file_path=data["file_path"] if isinstance(data["file_path"], str) else None,
        line_start=data["line_start"] if isinstance(data["line_start"], int) else None,
        line_end=data["line_end"] if isinstance(data["line_end"], int) else None,
    )
