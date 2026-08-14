"""Running one specialist: retrieve, prompt, parse.

The parser is the interesting part. Model output is untrusted in the ordinary sense (it may
be malformed) *and* in the security sense (it may be steered by injected diff content), so
everything here is defensive:

* A response that will not parse drops **that specialist only**, at WARN. The other two still
  produce a review — one bad actor does not fail the run.
* Every field is coerced and bounds-checked. A `severity` of `"catastrophic"` or a
  `confidence` of `7.5` is a malformed finding, not a very serious one.
* `chunk_id` is left as a raw ``str | None`` and is *not* validated here. Validation belongs
  to ``ground_candidates`` in the domain, which is the single place cite-or-drop is decided.
  Two places deciding it means two places to get it wrong.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from app.application.agents.prompts import (
    FINDINGS_SCHEMA,
    build_specialist_messages,
    render_references,
)
from app.domain.entities import CandidateFinding, ScoredChunk
from app.domain.ports import ChatModelPort, LoggerPort, RetrieverPort
from app.domain.values import RunId, Severity, SpecialistKind, TokenUsage

MAX_FINDINGS_PER_SPECIALIST = 8
"""A specialist returning 40 findings is not being thorough, it is pattern-matching noise.

Truncation is a blunt instrument, but an unbounded list also blows the synthesis prompt's
token budget, and the synthesis model is the expensive one.
"""

RETRIEVAL_QUERIES: dict[SpecialistKind, str] = {
    SpecialistKind.CORRECTNESS: "correctness invariants, error handling and edge case conventions",
    SpecialistKind.SECURITY: "security rules, authentication, secrets handling and untrusted input",
    SpecialistKind.TEST_COVERAGE: "testing rules, required coverage and what must have a test",
}


@dataclass(frozen=True, slots=True)
class SpecialistResult:
    specialist: SpecialistKind
    candidates: tuple[CandidateFinding, ...]
    offered: tuple[ScoredChunk, ...]
    usage: TokenUsage | None
    failed_reason: str | None = None

    @property
    def failed(self) -> bool:
        return self.failed_reason is not None


def build_query(specialist: SpecialistKind, *, symbols: Sequence[str], paths: Sequence[str]) -> str:
    """A specialist's retrieval query: its concern plus the symbols it saw.

    Deliberately *not* the whole diff. Embedding an entire diff retrieves whatever the diff is
    mostly about, which is usually the feature rather than the concern — the security
    specialist ends up with chunks about the feature and nothing about security.
    """
    parts = [RETRIEVAL_QUERIES[specialist]]
    if symbols:
        parts.append(" ".join(symbols[:8]))
    if paths:
        parts.append(" ".join(paths[:5]))
    return " ".join(parts)


def parse_candidates(raw: str, specialist: SpecialistKind) -> list[CandidateFinding]:
    """Parse model output into candidates, discarding anything malformed.

    Raises ``ValueError`` only when the response as a whole is unusable; individual bad
    findings are skipped so that one malformed entry does not lose the other seven.
    """
    text = raw.strip()
    if text.startswith("```"):
        # Models wrap JSON in fences despite being told not to. Cheap to tolerate.
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"response was not JSON: {exc}") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("findings"), list):
        raise ValueError("response had no 'findings' array")

    candidates: list[CandidateFinding] = []
    for item in payload["findings"][:MAX_FINDINGS_PER_SPECIALIST]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        body = str(item.get("body", "")).strip()
        if not title or not body:
            continue

        try:
            severity = Severity(str(item.get("severity", "")).strip().lower())
        except ValueError:
            # An out-of-enum severity is a malformed finding, not a very serious one.
            severity = Severity.LOW

        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = min(1.0, max(0.0, confidence))

        chunk_id = item.get("chunk_id")
        line_start = item.get("line_start")

        candidates.append(
            CandidateFinding(
                specialist=specialist,
                severity=severity,
                confidence=confidence,
                title=title[:300],
                body=body[:4000],
                # Left raw on purpose: cite-or-drop is decided in one place, in the domain.
                chunk_id=str(chunk_id).strip() if isinstance(chunk_id, str) else None,
                file_path=str(item["file_path"])
                if isinstance(item.get("file_path"), str)
                else None,
                line_start=int(line_start) if isinstance(line_start, int) else None,
            )
        )
    return candidates


async def run_specialist(
    specialist: SpecialistKind,
    *,
    run_id: RunId,
    pr_title: str,
    diff_excerpt: str,
    symbols: Sequence[str],
    paths: Sequence[str],
    repo: object,
    commit_sha: str,
    retriever: RetrieverPort,
    model: ChatModelPort,
    logger: LoggerPort,
    top_k: int,
    model_name: str | None = None,
) -> SpecialistResult:
    """Retrieve, prompt and parse for one specialist. Never raises."""
    from app.domain import log_events  # local import keeps the module list flat at the top

    logger.info(log_events.SPECIALIST_STARTED, specialist=specialist.value)

    query = build_query(specialist, symbols=symbols, paths=paths)
    try:
        offered = tuple(
            await retriever.retrieve(query, repo=repo, commit_sha=commit_sha, top_k=top_k)  # type: ignore[arg-type]
        )
    except Exception as exc:
        logger.warn(
            log_events.SPECIALIST_FAILED, specialist=specialist.value, reason=f"retrieval: {exc}"
        )
        return SpecialistResult(specialist, (), (), None, failed_reason=f"retrieval: {exc}")

    references = render_references(offered)
    messages = build_specialist_messages(
        specialist, pr_title=pr_title, diff_excerpt=diff_excerpt, references=references
    )

    try:
        completion = await model.complete(
            messages, node=specialist.value, model=model_name, json_schema=FINDINGS_SCHEMA
        )
    except Exception as exc:
        logger.warn(
            log_events.SPECIALIST_FAILED, specialist=specialist.value, reason=f"provider: {exc}"
        )
        return SpecialistResult(specialist, (), offered, None, failed_reason=f"provider: {exc}")

    try:
        candidates = parse_candidates(completion.content, specialist)
    except ValueError as exc:
        logger.warn(log_events.SPECIALIST_FAILED, specialist=specialist.value, reason=str(exc))
        return SpecialistResult(specialist, (), offered, completion.usage, failed_reason=str(exc))

    logger.info(
        log_events.SPECIALIST_COMPLETED,
        specialist=specialist.value,
        candidates=len(candidates),
        offered=len(offered),
        duration_ms=completion.usage.latency_ms,
    )
    return SpecialistResult(specialist, tuple(candidates), offered, completion.usage)
