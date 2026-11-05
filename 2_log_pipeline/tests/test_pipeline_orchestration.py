import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


PIPELINE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE_DIR))

import decoder
from decoder_adapter import payload_for_ai


def _import_pipeline_main():
    if "mysql.connector" not in sys.modules:
        mysql_module = types.ModuleType("mysql")
        mysql_module.__path__ = []
        connector_module = types.ModuleType("mysql.connector")
        connector_module.connect = Mock()
        mysql_module.connector = connector_module
        sys.modules["mysql"] = mysql_module
        sys.modules["mysql.connector"] = connector_module
    return importlib.import_module("pipeline_main")


pipeline_main = _import_pipeline_main()

from jsonl_parser import JSONLParseError, JSONLParser
from normalized_event import NormalizedEvent
from parser_dispatcher import UnknownFormatError


class PipelineOrchestrationTests(unittest.TestCase):
    def setUp(self):
        record = {
            "source_event_id": "0123456789abcdef0123456789abcdef",
            "timestamp": "2026-10-31T12:34:56+00:00",
            "source_ip": "192.0.2.10",
            "method": "POST",
            "uri": "/comment.php",
            "query_params": {"q": "áo dài"},
            "body_params": {"content": "raw"},
            "payload": {
                "query_params": {"q": "áo dài"},
                "body_params": {"content": "raw"},
            },
            "user_agent": "unit-test",
            "request_type": "web",
        }
        self.raw_record = json.dumps(record, ensure_ascii=False)

    def test_pipeline_registers_jsonl_parser(self):
        self.assertIsInstance(
            pipeline_main.PARSER_DISPATCHER._parsers["jsonl"], JSONLParser
        )

    def test_configured_format_is_explicitly_dispatched(self):
        with patch.object(pipeline_main, "LOG_FORMAT", "jsonl"), patch.object(
            pipeline_main.PARSER_DISPATCHER,
            "parse",
            wraps=pipeline_main.PARSER_DISPATCHER.parse,
        ) as parse:
            event = pipeline_main.parse_record(self.raw_record)

        self.assertIsInstance(event, NormalizedEvent)
        parse.assert_called_once_with("jsonl", self.raw_record)

    def test_jsonl_parse_error_propagates_unchanged(self):
        with self.assertRaises(JSONLParseError):
            pipeline_main.parse_record('{"malformed":')

    def test_unknown_configured_format_does_not_fallback(self):
        with patch.object(pipeline_main, "LOG_FORMAT", "legacy"), patch.object(
            pipeline_main.requests, "post"
        ) as post:
            with self.assertRaises(UnknownFormatError):
                pipeline_main.process_line(self.raw_record)

        post.assert_not_called()

    def test_system_errors_are_not_reclassified_as_parse_errors(self):
        with tempfile.TemporaryDirectory() as state_directory:
            with patch.object(
                pipeline_main,
                "REJECTED_FILE_PATH",
                str(Path(state_directory) / "rejected.jsonl"),
            ), patch.object(
                pipeline_main.requests,
                "post",
                side_effect=pipeline_main.requests.ConnectionError(
                    "AI unavailable"
                ),
            ), patch.object(
                pipeline_main.mysql.connector, "connect"
            ) as connect, patch("builtins.print"):
                result = pipeline_main.process_line(self.raw_record)

        self.assertEqual(result.status, pipeline_main.AIStatus.AI_RETRY)
        connect.assert_not_called()

    def test_raw_record_is_preserved_and_payload_is_serialized_for_decoder(self):
        with tempfile.TemporaryDirectory() as state_directory:
            with patch.object(
                pipeline_main,
                "REJECTED_FILE_PATH",
                str(Path(state_directory) / "rejected.jsonl"),
            ), patch.object(
                pipeline_main,
                "payload_for_ai",
                wraps=payload_for_ai,
            ) as adapter, patch.object(
                pipeline_main,
                "analyze_with_ai",
                wraps=pipeline_main.analyze_with_ai,
            ) as analyze, patch.object(
                decoder, "decode_payload"
            ) as decode, patch.object(
                pipeline_main.requests,
                "post",
                side_effect=pipeline_main.requests.ConnectionError(
                    "AI unavailable"
                ),
            ), patch.object(
                pipeline_main.mysql.connector, "connect"
            ) as connect, patch("builtins.print"):
                event = pipeline_main.parse_record(self.raw_record)
                result = pipeline_main.process_line(self.raw_record)

        expected_payload = json.dumps(
            event.payload_raw, ensure_ascii=False, separators=(",", ":")
        )
        self.assertEqual(event.raw_record, self.raw_record)
        self.assertEqual(result.status, pipeline_main.AIStatus.AI_RETRY)
        connect.assert_not_called()
        adapter.assert_called_once()
        adapted_event = adapter.call_args.args[0]
        self.assertEqual(adapted_event.raw_record, self.raw_record)
        self.assertEqual(adapted_event.payload_raw, event.payload_raw)
        self.assertIn("áo dài", expected_payload)
        self.assertEqual(analyze.call_args.args[0], expected_payload)
        decode.assert_not_called()
        self.assertIsNone(event.payload_decoded)


if __name__ == "__main__":
    unittest.main()
