"""The real tracer. ``TracerPort`` had exactly one implementation before this file:
``NullTracer``, a test fake that does nothing. Every phase's tests passed against it because a
test doesn't care whether a span was actually recorded -- it cares whether the *code path*
under the span ran. That silence is exactly why this gap could sit unnoticed through eight
phases: nothing was broken, nothing was missing from any test's perspective, and there was
still no way to answer "how long did the security specialist actually take" from a real run.

``docs/Design.md`` draws a specific line: *"if traces are logs you cannot aggregate cost."*
This does not reach for a separate tracing backend (OpenTelemetry, Jaeger) to earn that
distinction -- a span here is a structured log line with a ``duration_ms`` field and a
``span_id`` linking its start to its end, aggregable by any tool that can group JSON lines by
field, which is what this project's actual scale needs. Reaching for a full OTel SDK now would
be solving a problem -- distributed tracing across services -- that a single-process
supervisor agent does not have.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from app.domain import log_events
from app.domain.ports import LoggerPort


class StructlogTracer:
    """Adapter satisfying ``TracerPort``, backed by structured log lines rather than a
    separate tracing protocol -- see the module docstring for why that's the right amount of
    machinery here."""

    def __init__(self, logger: LoggerPort) -> None:
        self._logger = logger

    @contextmanager
    def span(self, name: str, **attributes: object) -> Iterator[None]:
        span_id = uuid.uuid4().hex[:16]
        started = time.perf_counter()
        self._logger.debug(log_events.SPAN_STARTED, span=name, span_id=span_id, **attributes)
        try:
            yield
        except Exception as exc:
            self._logger.warn(
                log_events.SPAN_FAILED,
                span=name,
                span_id=span_id,
                duration_ms=int((time.perf_counter() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}"[:300],
            )
            raise
        else:
            self._logger.debug(
                log_events.SPAN_COMPLETED,
                span=name,
                span_id=span_id,
                duration_ms=int((time.perf_counter() - started) * 1000),
                **attributes,
            )
