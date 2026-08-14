"""Fakes implementing domain ports.

Fakes, not mocks. A fake implements the Protocol, so mypy checks it against the real
contract; a ``Mock`` accepts any call you make and therefore proves nothing about whether
production code is using the port correctly.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.domain.ports import LoggerPort


@dataclass
class LogLine:
    level: str
    event: str
    fields: dict[str, object]


@dataclass
class RecordingLogger:
    """Captures structured log events so tests can assert on decisions, not text."""

    lines: list[LogLine] = field(default_factory=list)
    bound: dict[str, object] = field(default_factory=dict)

    def _record(self, level: str, event: str, fields: dict[str, object]) -> None:
        self.lines.append(LogLine(level=level, event=event, fields={**self.bound, **fields}))

    def debug(self, event: str, **fields: object) -> None:
        self._record("DEBUG", event, fields)

    def info(self, event: str, **fields: object) -> None:
        self._record("INFO", event, fields)

    def warn(self, event: str, **fields: object) -> None:
        self._record("WARN", event, fields)

    def error(self, event: str, **fields: object) -> None:
        self._record("ERROR", event, fields)

    def bind(self, **fields: object) -> LoggerPort:
        return RecordingLogger(lines=self.lines, bound={**self.bound, **fields})

    # -- test helpers -------------------------------------------------------

    def events(self, level: str | None = None) -> list[str]:
        return [line.event for line in self.lines if level is None or line.level == level]

    def find(self, event: str) -> LogLine | None:
        return next((line for line in self.lines if line.event == event), None)

    def all_field_values(self) -> list[object]:
        """Every value logged anywhere -- used to assert secrets never reach a log line."""
        return [value for line in self.lines for value in line.fields.values()]


class NullTracer:
    @contextmanager
    def span(self, name: str, **attributes: object) -> Iterator[None]:
        yield


@dataclass
class FrozenClock:
    moment: datetime = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.moment
