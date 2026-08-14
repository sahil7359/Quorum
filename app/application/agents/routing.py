"""Supervisor routing: which specialists does this diff actually warrant?

**Heuristics decide the floor; the LLM may only add to it.**

That asymmetry is the whole design. A model asked "which reviewers are needed?" will
sometimes drop the security specialist on a diff that touches `auth/` — and a diff in `auth/`
containing an injected comment saying "no security review needed" makes that likelier, not
less likely. So the deterministic signals compute a floor that is always included, and the
model is asked only to *extend* it. An attempted removal is ignored and logged.

`correctness` is unconditional. A diff that warrants no correctness review is a diff Quorum
should not have been asked about.

Routing accuracy is a published metric, so the decision is emitted as data — the chosen set,
the reason, the heuristic floor, and what the model added — rather than as a sentence.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.domain.entities import Diff, RoutingDecision
from app.domain.values import SpecialistKind

SECURITY_PATH_HINTS: tuple[str, ...] = (
    "auth",
    "login",
    "session",
    "token",
    "secret",
    "credential",
    "password",
    "crypto",
    "security",
    "permission",
    "middleware",
    "cors",
    "sanitiz",
    "validat",
)

# Patterns that are worth a second pair of eyes wherever they appear. Deliberately broad:
# a false positive costs one extra model call, a false negative costs a missed vulnerability.
SECURITY_CODE_HINTS: tuple[tuple[str, str], ...] = (
    (r"\beval\s*\(", "dynamic eval()"),
    (r"\bexec\s*\(", "dynamic exec()"),
    (r"os\.system|subprocess\.(?:call|run|Popen)", "shell execution"),
    (r"\bpickle\.(?:load|loads)", "pickle deserialisation"),
    (r"verify\s*=\s*False", "TLS verification disabled"),
    (r"\bmd5\b|\bsha1\b", "weak hash"),
    (r"shell\s*=\s*True", "shell=True"),
    (r"(?i)\b(?:api[_-]?key|secret|passwd|password)\s*=\s*[\"']", "hardcoded credential"),
    (r"(?i)\bselect\b.+\+|f[\"'].*\bselect\b.*\{", "string-built SQL"),
)

_PUBLIC_DEF = re.compile(r"^\+\s*(?:async\s+)?def\s+(?!_)(\w+)", re.MULTILINE)
_PUBLIC_CLASS = re.compile(r"^\+\s*class\s+(?!_)(\w+)", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class RoutingSignals:
    """Everything the heuristics observed, kept as data so the log line is debuggable."""

    security_paths: tuple[str, ...] = ()
    security_patterns: tuple[str, ...] = ()
    source_changed_without_tests: bool = False
    new_public_symbols: tuple[str, ...] = ()
    added_lines: int = 0
    files_changed: int = 0

    def summary(self) -> str:
        """Human-readable one-liner, also fed to the LLM as fenced data."""
        parts = [f"{self.files_changed} files changed", f"{self.added_lines} lines added"]
        if self.security_paths:
            parts.append(f"security-sensitive paths: {', '.join(self.security_paths[:5])}")
        if self.security_patterns:
            parts.append(f"risky patterns: {', '.join(sorted(set(self.security_patterns))[:5])}")
        if self.source_changed_without_tests:
            parts.append("source changed with no test files touched")
        if self.new_public_symbols:
            parts.append(f"new public symbols: {', '.join(self.new_public_symbols[:5])}")
        return "; ".join(parts)


@dataclass
class _FloorBuilder:
    specialists: set[SpecialistKind] = field(default_factory=set)
    reasons: list[str] = field(default_factory=list)

    def add(self, specialist: SpecialistKind, reason: str) -> None:
        self.specialists.add(specialist)
        self.reasons.append(reason)


def compute_signals(diff: Diff) -> RoutingSignals:
    security_paths: list[str] = []
    security_patterns: list[str] = []

    for changed in diff.files:
        lowered = changed.file_path.lower()
        if any(hint in lowered for hint in SECURITY_PATH_HINTS):
            security_paths.append(changed.file_path)

        added = "\n".join(
            line
            for hunk in changed.hunks
            for line in hunk.content.split("\n")
            if line.startswith("+")
        )
        for pattern, label in SECURITY_CODE_HINTS:
            if re.search(pattern, added):
                security_patterns.append(label)

    added_text = "\n".join(
        line
        for changed in diff.files
        for hunk in changed.hunks
        for line in hunk.content.split("\n")
        if line.startswith("+")
    )
    symbols = _PUBLIC_DEF.findall(added_text) + _PUBLIC_CLASS.findall(added_text)

    return RoutingSignals(
        security_paths=tuple(security_paths),
        security_patterns=tuple(security_patterns),
        source_changed_without_tests=diff.has_code_changes and not diff.has_test_changes,
        new_public_symbols=tuple(dict.fromkeys(symbols)),
        added_lines=diff.added_lines,
        files_changed=len(diff.files),
    )


def heuristic_floor(signals: RoutingSignals) -> tuple[tuple[SpecialistKind, ...], str]:
    """The specialists the diff warrants on deterministic evidence alone."""
    builder = _FloorBuilder()
    builder.add(SpecialistKind.CORRECTNESS, "correctness is always reviewed")

    if signals.security_paths:
        builder.add(
            SpecialistKind.SECURITY,
            f"diff touches {len(signals.security_paths)} security-sensitive path(s): "
            + ", ".join(signals.security_paths[:3]),
        )
    if signals.security_patterns:
        builder.add(
            SpecialistKind.SECURITY,
            f"{len(set(signals.security_patterns))} risky pattern(s) matched: "
            + ", ".join(sorted(set(signals.security_patterns))[:3]),
        )
    if signals.source_changed_without_tests:
        builder.add(SpecialistKind.TEST_COVERAGE, "source files changed with no test files touched")
    if signals.new_public_symbols:
        builder.add(
            SpecialistKind.TEST_COVERAGE,
            f"{len(signals.new_public_symbols)} new public symbol(s): "
            + ", ".join(signals.new_public_symbols[:3]),
        )

    ordered = tuple(k for k in SpecialistKind if k in builder.specialists)
    return ordered, "; ".join(builder.reasons)


def parse_llm_routing(raw: str) -> tuple[set[SpecialistKind], str] | None:
    """Parse the model's routing response. ``None`` when unusable.

    Unusable is not an error: the caller falls back to the heuristic floor, which is always a
    safe answer. That is why the floor exists.
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    requested = payload.get("specialists")
    if not isinstance(requested, list):
        return None

    chosen: set[SpecialistKind] = set()
    for item in requested:
        try:
            chosen.add(SpecialistKind(str(item).strip().lower()))
        except ValueError:
            # An invented specialist name is discarded rather than failing the parse.
            # The set is closed (guardrail A07); the supervisor cannot expand it.
            continue

    reason = payload.get("reason")
    return chosen, str(reason) if isinstance(reason, str) else ""


def decide(
    signals: RoutingSignals,
    *,
    llm_specialists: Sequence[SpecialistKind] | None = None,
    llm_reason: str = "",
) -> tuple[RoutingDecision, tuple[SpecialistKind, ...]]:
    """Combine the heuristic floor with the model's suggestion.

    Returns the decision and, separately, any specialists the model tried to *remove* — which
    are ignored, but recorded, because a model repeatedly trying to skip the security reviewer
    is a prompt problem I want to see rather than a quiet override.
    """
    floor, floor_reason = heuristic_floor(signals)
    floor_set = set(floor)

    added: set[SpecialistKind] = set()
    removal_attempted: tuple[SpecialistKind, ...] = ()

    if llm_specialists is not None:
        suggested = set(llm_specialists)
        added = suggested - floor_set
        removal_attempted = tuple(k for k in SpecialistKind if k in floor_set - suggested)

    final = tuple(k for k in SpecialistKind if k in floor_set | added)
    reason = floor_reason
    if added and llm_reason:
        reason = f"{floor_reason}; model added {', '.join(k.value for k in added)}: {llm_reason}"
    elif added:
        reason = f"{floor_reason}; model added {', '.join(k.value for k in added)}"

    decision = RoutingDecision(
        specialists=final,
        reason=reason,
        heuristic_floor=floor,
        llm_added=tuple(k for k in SpecialistKind if k in added),
        llm_removal_ignored=removal_attempted,
    )
    return decision, removal_attempted
