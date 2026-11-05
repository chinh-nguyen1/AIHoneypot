import json
import sys
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE_DIR))

from jsonl_parser import JSONLParseError, JSONLParser
from normalized_event import NormalizedEvent
from parser_dispatcher import (
    DuplicateParserError,
    InvalidParserError,
    ParserDispatcher,
    UnknownFormatError,
)


class ParserDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.record = {
            "source_event_id": "0123456789abcdef0123456789abcdef",
            "timestamp": "2026-10-31T12:34:56+00:00",
            "source_ip": None,
            "method": "POST",
            "uri": "/comment.php",
            "query_params": {"q": "áo dài"},
            "body_params": {"content": "raw"},
            "payload": {
                "query_params": {"q": "áo dài"},
                "body_params": {"content": "raw"},
            },
            "user_agent": None,
            "request_type": "web",
        }
        self.raw_record = json.dumps(self.record, ensure_ascii=False)
        self.dispatcher = ParserDispatcher()

    def test_registers_jsonl_parser(self):
        self.dispatcher.register("jsonl", JSONLParser())

        self.assertIn("jsonl", self.dispatcher._parsers)

    def test_dispatch_returns_normalized_event(self):
        self.dispatcher.register("jsonl", JSONLParser())

        event = self.dispatcher.parse("jsonl", self.raw_record)

        self.assertIsInstance(event, NormalizedEvent)
        self.assertEqual(event.source_format, "jsonl")

    def test_format_name_is_normalized_consistently(self):
        self.dispatcher.register(" JSONL ", JSONLParser())

        event = self.dispatcher.parse("jsonl", self.raw_record)

        self.assertIsInstance(event, NormalizedEvent)
        with self.assertRaises(DuplicateParserError):
            self.dispatcher.register("jsonl", JSONLParser())

    def test_rejects_duplicate_format_registration(self):
        self.dispatcher.register("jsonl", JSONLParser())

        with self.assertRaises(DuplicateParserError):
            self.dispatcher.register("jsonl", JSONLParser())

    def test_rejects_parser_without_parse_method(self):
        with self.assertRaises(InvalidParserError):
            self.dispatcher.register("jsonl", object())

    def test_rejects_parse_method_with_incompatible_signature(self):
        class IncompatibleParser:
            def parse(self) -> NormalizedEvent:
                return NormalizedEvent(source_format="fake", raw_record="")

        with self.assertRaises(InvalidParserError):
            self.dispatcher.register("invalid", IncompatibleParser())

    def test_rejects_parser_returning_wrong_type(self):
        class InvalidResultParser:
            def parse(self, raw_record: str) -> NormalizedEvent:
                return str(raw_record)

        self.dispatcher.register("invalid", InvalidResultParser())

        with self.assertRaises(TypeError):
            self.dispatcher.parse("invalid", self.raw_record)

    def test_unknown_format_raises_clear_dispatch_error(self):
        with self.assertRaisesRegex(UnknownFormatError, "unknown"):
            self.dispatcher.parse("unknown", self.raw_record)

    def test_preserves_jsonl_parse_error_without_fallback(self):
        class MustNotBeCalledParser:
            def parse(self, raw_record: str) -> NormalizedEvent:
                raise AssertionError(raw_record)

        self.dispatcher.register("jsonl", JSONLParser())
        self.dispatcher.register("alternate", MustNotBeCalledParser())

        with self.assertRaises(JSONLParseError):
            self.dispatcher.parse("jsonl", '{"malformed":')

    def test_raw_record_is_unchanged_and_payload_is_not_decoded(self):
        raw_record = " \t" + self.raw_record + "  "
        self.dispatcher.register("jsonl", JSONLParser())

        event = self.dispatcher.parse("jsonl", raw_record)

        self.assertEqual(event.raw_record, raw_record)
        self.assertIsNone(event.payload_decoded)
        self.assertEqual(event.payload_raw, self.record["payload"])

    def test_invalid_format_names_are_rejected(self):
        for value in ("", " \t ", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.dispatcher.register(value, JSONLParser())


if __name__ == "__main__":
    unittest.main()
