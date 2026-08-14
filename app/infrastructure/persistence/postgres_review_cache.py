"""The review cache, Postgres edition. Same behaviour as ``persistence/review_cache.py``
(SQLite); the reason a Postgres version exists at all is durability across restarts. Render's
free tier disk is ephemeral -- a SQLite file there does not survive a redeploy, which would
silently break the "a cached gallery keeps working" claim the whole point of caching makes.
See ``postgres_audit.py``'s module docstring for why this wraps sync psycopg in threads
rather than using psycopg's async mode.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any, Self

import psycopg
from psycopg.rows import dict_row

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
    payload      JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS review_cache_repo_pr_idx
    ON review_cache (repo, pr_number, created_at);
"""


class PostgresReviewCache:
    """Adapter satisfying ``ReviewCachePort``."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    @classmethod
    async def connect(cls, dsn: str) -> Self:
        return await asyncio.to_thread(cls._connect_sync, dsn)

    @classmethod
    def _connect_sync(cls, dsn: str) -> Self:
        connection = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        with connection.cursor() as cursor:
            cursor.execute(SCHEMA)
        return cls(connection)

    async def get(self, cache_key: str) -> Review | None:
        return await asyncio.to_thread(self._get_sync, cache_key)

    def _get_sync(self, cache_key: str) -> Review | None:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT payload FROM review_cache WHERE cache_key = %s", (cache_key,))
            row = cursor.fetchone()
        return _review_from_json(row["payload"]) if row is not None else None

    async def get_latest(self, repo: RepoRef, pr_number: int) -> Review | None:
        return await asyncio.to_thread(self._get_latest_sync, repo, pr_number)

    def _get_latest_sync(self, repo: RepoRef, pr_number: int) -> Review | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT payload FROM review_cache WHERE repo = %s AND pr_number = %s "
                "ORDER BY created_at DESC LIMIT 1",
                (str(repo), pr_number),
            )
            row = cursor.fetchone()
        return _review_from_json(row["payload"]) if row is not None else None

    async def put(self, cache_key: str, review: Review) -> None:
        await asyncio.to_thread(self._put_sync, cache_key, review)

    def _put_sync(self, cache_key: str, review: Review) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO review_cache "
                "(cache_key, run_id, repo, pr_number, head_sha, payload, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (cache_key) DO UPDATE SET payload = EXCLUDED.payload, "
                "created_at = EXCLUDED.created_at",
                (
                    cache_key,
                    str(review.run_id),
                    str(review.repo),
                    review.pr_number,
                    review.head_sha,
                    json.dumps(_review_to_json(review)),
                    datetime.now(UTC),
                ),
            )

    async def close(self) -> None:
        await asyncio.to_thread(self._connection.close)


def _review_to_json(review: Review) -> dict[str, Any]:
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


def _finding_to_json(finding: Finding) -> dict[str, Any]:
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
