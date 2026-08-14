"""The real logger. ``structlog`` has been a declared dependency since Phase 0 and, until this
module, had never actually been imported outside a test fixture -- every phase built and
tested against ``RecordingLogger`` (a fake that just appends to a list) and nothing ever wired
a real one in. Same category of gap as the redaction module next to this one: a dependency
existing in ``pyproject.toml`` is not the same claim as a dependency being used.

Every field passed to every log call is redacted before it reaches structlog's processor
chain -- see ``redaction.py`` for what that catches and, just as importantly, what it
deliberately does not (a `run_id` or commit SHA is not a secret, and log lines built around
those ids are exactly what "reconstructable end to end from logs alone" depends on).
"""

from __future__ import annotations

import logging
import sys

import structlog
from structlog.typing import FilteringBoundLogger

from app.domain.ports import LoggerPort
from app.infrastructure.observability.redaction import redact_fields


def configure_structlog(*, log_level: str, log_format: str) -> None:
    """Called once, at process startup, by the composition root. Not called by any test --
    tests build a :class:`StructlogLogger` against whatever configuration (or lack of it) the
    test process already has, because re-configuring global structlog state from inside a test
    would make one test's logging setup leak into the next.
    """
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if log_format == "json"
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(log_level, logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


class StructlogLogger:
    """Adapter satisfying ``LoggerPort``."""

    def __init__(self, logger: FilteringBoundLogger | None = None) -> None:
        self._logger: FilteringBoundLogger = (
            logger if logger is not None else structlog.get_logger()
        )

    def debug(self, event: str, **fields: object) -> None:
        self._logger.debug(event, **redact_fields(fields))

    def info(self, event: str, **fields: object) -> None:
        self._logger.info(event, **redact_fields(fields))

    def warn(self, event: str, **fields: object) -> None:
        self._logger.warning(event, **redact_fields(fields))

    def error(self, event: str, **fields: object) -> None:
        self._logger.error(event, **redact_fields(fields))

    def bind(self, **fields: object) -> LoggerPort:
        # why: redacted here too, not only at the terminal debug/info/warn/error calls --
        #      a run_id or repo name bound once at the top of a request is safe (see
        #      redaction's identifier exclusions), but nothing stops a future caller from
        #      binding something secret-shaped, and bind() is as much an emit boundary as
        #      the four level methods are.
        return StructlogLogger(self._logger.bind(**redact_fields(fields)))
