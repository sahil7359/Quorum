"""Secret redaction. Guardrail G10, and the second half of T4 in ``docs/Security.md``.

Applied at the **logger's emit boundary**, not at each call site. A call site trusted to
remember redaction is a call site that will eventually forget it -- the same reasoning behind
``TracedNode`` making instrumentation structural rather than a convention every node author has
to recall. Every field value passed to a real logger goes through :func:`redact_value` before
it reaches structlog, so a secret reaching a log line is a bug in this module, not in whichever
node happened to log it.

Patterns matched, each with the incident it exists to prevent:

- GitHub PATs (``ghp_``, ``github_pat_``) and Groq keys (``gsk_``) -- the two credential
  families this project actually holds.
- Bearer/authorization header values -- catches a credential logged incidentally via an HTTP
  client's request/response logging, not just ones this codebase constructs directly.
- Generic high-entropy runs of 32+ alphanumeric characters -- catches a credential shape this
  project has never seen before. A provider's token format changing, or a new provider being
  added, should not require a matching change here to stay covered.
"""

from __future__ import annotations

import re
from typing import Any

_NAMED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
)

_GENERIC_RUN = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")

_UUID_SHAPE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_HEX_ONLY = re.compile(r"^[0-9a-f]+$", re.IGNORECASE)

MASK = "[REDACTED]"


def _looks_like_a_legitimate_identifier(candidate: str) -> bool:
    """UUIDs (``run_id``, ``finding_id``) and hex digests (commit SHAs, chunk ids) are not
    secrets -- they are exactly the correlation ids this project's logging exists to carry.
    Excluding them by shape, rather than by field name, means the exclusion holds even for an
    identifier logged somewhere unexpected, not just the field names known about today.

    Real secrets this project holds (``ghp_...``, ``gsk_...``) mix case and are not
    UUID-shaped or pure hex, so this exclusion does not create a gap for them -- it is caught
    by :data:`_NAMED_PATTERNS` first, and this function only runs on the generic fallback.
    """
    stripped = candidate.replace("-", "")
    return bool(_UUID_SHAPE.match(candidate)) or bool(_HEX_ONLY.match(stripped))


def _redact_generic_high_entropy(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        candidate = match.group()
        return candidate if _looks_like_a_legitimate_identifier(candidate) else MASK

    return _GENERIC_RUN.sub(replace, text)


def redact_text(text: str) -> str:
    """Replace every matched secret-shaped substring in ``text`` with :data:`MASK`.

    Named patterns (known token prefixes) run first and are unconditional -- a real GitHub or
    Groq token is always redacted regardless of shape overlap with an identifier. The generic
    high-entropy fallback runs last and is conditional, so it catches an unknown-shaped secret
    without also catching a `run_id`, `finding_id`, or commit SHA -- see
    :func:`_looks_like_a_legitimate_identifier` for exactly what it lets through and why.
    """
    redacted = text
    for pattern in _NAMED_PATTERNS:
        redacted = pattern.sub(MASK, redacted)
    return _redact_generic_high_entropy(redacted)


def redact_value(value: Any) -> Any:
    """Recurse into dicts/lists/tuples; redact strings; pass everything else through
    unchanged. Structured log fields are frequently nested (a list of chunk ids, a dict of
    signals), and a redactor that only handles the top level misses exactly the cases where a
    secret ends up three levels deep in a debug payload."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact_value(val) for key, val in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    return value


def redact_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Redact every value in a structured-log field mapping. Keys are never redacted -- a
    field named ``token`` staying named ``token`` is useful; a field's *value* never should be
    the secret itself."""
    return {key: redact_value(val) for key, val in fields.items()}
