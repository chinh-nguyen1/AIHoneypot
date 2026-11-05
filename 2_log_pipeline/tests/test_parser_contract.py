import json
import sys
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE_DIR))

from jsonl_parser import (
    JSONLParseError,
    JSONLParser,
    NormalizedEvent,
    parse_jsonl_record,
)
from normalized_event import NormalizedEvent as SharedNormalizedEvent
from parser_contract import LogParseError, LogParser


class ParserContractTests(unittest.TestCase):
    def setUp(self):
        self.record = {
            "source_event_id": "0123456789abcdef0123456789abcdef",
            "timestamp": "2026-10-31T12:34:56+00:00",
            "source_ip": None,
            "method": "POST",
            "uri": "/comment.php",
            "query_params": {},
            "body_params": {"content": "nội dung"},
            "payload": {
                "query_params": {},
                "body_params": {"content": "nội dung"},
            },
            "user_agent": None,
            "request_type": "web",
        }

    def test_jsonl_parser_satisfies_protocol_structurally(self):
        parser: LogParser = JSONLParser()

        self.assertIsInstance(parser, LogParser)

    def test_protocol_typed_parser_returns_normalized_event(self):
        parser: LogParser = JSONLParser()
        raw_record = json.dumps(self.record, ensure_ascii=False)

        event = parser.parse(raw_record)

        self.assertIsInstance(event, NormalizedEvent)
        self.assertIs(type(event), SharedNormalizedEvent)
        self.assertEqual(event.source_format, "jsonl")

    def test_jsonl_parse_error_is_shared_log_parse_error(self):
        self.assertTrue(issubclass(JSONLParseError, LogParseError))

        with self.assertRaises(LogParseError):
            JSONLParser().parse('{"incomplete":')

    def test_existing_parse_function_and_exception_contract_remain_available(self):
        raw_record = json.dumps(self.record, ensure_ascii=False)

        event = parse_jsonl_record(raw_record)

        self.assertIsInstance(event, NormalizedEvent)
        with self.assertRaises(JSONLParseError):
            parse_jsonl_record("[]")

    def test_parser_preserves_raw_record_and_does_not_decode_payload(self):
        raw_record = "  " + json.dumps(self.record, ensure_ascii=False) + "  "

        event = JSONLParser().parse(raw_record)

        self.assertEqual(event.raw_record, raw_record)
        self.assertIsNone(event.payload_decoded)
        self.assertEqual(event.payload_raw, self.record["payload"])

    def test_jsonl_validation_errors_use_the_shared_exception_type(self):
        invalid_timestamp = dict(self.record)
        invalid_timestamp["timestamp"] = "not-a-timestamp"
        invalid_id = dict(self.record)
        invalid_id["source_event_id"] = "INVALID"

        for invalid_record in (invalid_timestamp, invalid_id):
            with self.subTest(invalid_record=invalid_record):
                raw_record = json.dumps(invalid_record)
                with self.assertRaises(LogParseError):
                    JSONLParser().parse(raw_record)


if __name__ == "__main__":
    unittest.main()
