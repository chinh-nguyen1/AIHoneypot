"""Shared parser interface and parse-error base class."""

from typing import Protocol, runtime_checkable

from normalized_event import NormalizedEvent


class LogParseError(ValueError):
    """Base exception for malformed or unsupported log records."""


@runtime_checkable
class LogParser(Protocol):
    """Structural interface implemented by log-format parsers."""

    def parse(self, raw_record: str) -> NormalizedEvent:
        """Parse one complete record into a normalized event."""
        ...
