"""Dispatch complete log records to parsers registered by format name."""

from inspect import signature
from typing import Dict

from normalized_event import NormalizedEvent
from parser_contract import LogParser


class ParserDispatcherError(Exception):
    """Base exception for parser registration and dispatch configuration errors."""


class UnknownFormatError(ParserDispatcherError):
    """Raised when no parser is registered for the requested format."""


class DuplicateParserError(ParserDispatcherError):
    """Raised when a parser is already registered for a format name."""


class InvalidParserError(ParserDispatcherError):
    """Raised when a parser does not satisfy the LogParser protocol."""


class ParserDispatcher:
    """Register parsers by normalized format name and dispatch records to them."""

    def __init__(self) -> None:
        self._parsers: Dict[str, LogParser] = {}

    @staticmethod
    def _normalize_format_name(format_name: str) -> str:
        if not isinstance(format_name, str):
            raise ValueError("Format name must be a non-empty string")

        normalized = format_name.strip().lower()
        if not normalized:
            raise ValueError("Format name must be a non-empty string")
        return normalized

    def register(self, format_name: str, parser: LogParser) -> None:
        """Register a parser, rejecting invalid parsers and duplicate names."""
        normalized_name = self._normalize_format_name(format_name)

        if not isinstance(parser, LogParser):
            raise InvalidParserError(
                "Parser must provide parse(raw_record: str) -> NormalizedEvent"
            )
        try:
            signature(parser.parse).bind("record")
        except (TypeError, ValueError) as exc:
            raise InvalidParserError(
                "Parser parse method must accept one raw_record argument"
            ) from exc

        if normalized_name in self._parsers:
            raise DuplicateParserError(
                f"A parser is already registered for format '{normalized_name}'"
            )

        self._parsers[normalized_name] = parser

    def parse(self, format_name: str, raw_record: str) -> NormalizedEvent:
        """Dispatch a record to its explicitly selected parser."""
        normalized_name = self._normalize_format_name(format_name)
        try:
            parser = self._parsers[normalized_name]
        except KeyError as exc:
            raise UnknownFormatError(
                f"No parser is registered for format '{normalized_name}'"
            ) from exc

        event = parser.parse(raw_record)
        if not isinstance(event, NormalizedEvent):
            raise TypeError("Parser must return a NormalizedEvent")
        return event
