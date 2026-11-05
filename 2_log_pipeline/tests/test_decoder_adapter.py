import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


PIPELINE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE_DIR))

import decoder
from decoder_adapter import payload_for_ai
from normalized_event import NormalizedEvent


class DecoderAdapterTests(unittest.TestCase):
    def make_event(self, payload, **overrides):
        values = {
            "source_format": "jsonl",
            "raw_record": '{"original":true}',
            "payload_raw": payload,
        }
        values.update(overrides)
        return NormalizedEvent(**values)

    def test_serializes_supported_json_value_types_without_coercion(self):
        cases = (
            {"query_params": {"q": "value"}, "body_params": {"id": 3}},
            ["item", 7, None],
            "plain string",
            19,
            3.25,
            True,
            None,
        )
        for payload in cases:
            with self.subTest(payload_type=type(payload).__name__):
                result = payload_for_ai(self.make_event(payload))

                self.assertIsInstance(result, str)
                self.assertEqual(json.loads(result), payload)
                self.assertEqual(
                    result,
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                )

    def test_unicode_is_preserved_in_serialized_json(self):
        result = payload_for_ai(self.make_event({"content": "áo dài"}))

        self.assertIn("áo dài", result)
        self.assertNotIn("\\u00e1", result)

    def test_percent_encoded_text_is_not_url_decoded(self):
        payload = {"value": "%2F%41"}

        result = payload_for_ai(self.make_event(payload))

        self.assertEqual(result, '{"value":"%2F%41"}')
        self.assertEqual(json.loads(result), payload)

    def test_base64_looking_text_is_not_decoded(self):
        payload = {"value": "test"}

        result = payload_for_ai(self.make_event(payload))

        self.assertEqual(result, '{"value":"test"}')

    def test_legacy_decoder_is_not_called_for_jsonl_events(self):
        with patch.object(decoder, "decode_payload") as decode:
            result = payload_for_ai(self.make_event({"value": "%2F test"}))

        decode.assert_not_called()
        self.assertEqual(result, '{"value":"%2F test"}')

    def test_event_payload_and_raw_record_are_not_modified(self):
        payload = {"nested": ["áo", "%2F", "test"]}
        event = self.make_event(payload)
        original_raw_record = event.raw_record
        original_payload = event.payload_raw

        payload_for_ai(event)

        self.assertIs(event.payload_raw, original_payload)
        self.assertEqual(event.payload_raw, payload)
        self.assertEqual(event.raw_record, original_raw_record)
        self.assertIsNone(event.payload_decoded)

    def test_unsupported_format_is_not_guessed_or_decoded(self):
        event = self.make_event("not verified", source_format="legacy")

        with patch.object(decoder, "decode_payload") as decode:
            with self.assertRaisesRegex(ValueError, "No payload preparation"):
                payload_for_ai(event)

        decode.assert_not_called()

    def test_pipeline_sends_adapter_output_to_ai_analysis(self):
        import importlib
        import types

        if "mysql.connector" not in sys.modules:
            mysql_module = types.ModuleType("mysql")
            mysql_module.__path__ = []
            connector_module = types.ModuleType("mysql.connector")
            connector_module.connect = Mock()
            mysql_module.connector = connector_module
            sys.modules["mysql"] = mysql_module
            sys.modules["mysql.connector"] = connector_module

        pipeline_main = importlib.import_module("pipeline_main")
        record = {
            "source_event_id": "0123456789abcdef0123456789abcdef",
            "timestamp": "2026-10-31T12:34:56+00:00",
            "source_ip": "192.0.2.10",
            "method": "POST",
            "uri": "/comment.php",
            "query_params": {"q": "%2F"},
            "body_params": {"content": "test"},
            "payload": {
                "query_params": {"q": "%2F"},
                "body_params": {"content": "test"},
            },
            "user_agent": "unit-test",
            "request_type": "web",
        }
        raw_record = json.dumps(record, ensure_ascii=False) + "\n"
        expected_payload = json.dumps(
            record["payload"], ensure_ascii=False, separators=(",", ":")
        )
        analysis_result = pipeline_main.AIAnalysisResult(
            status=pipeline_main.AIStatus.AI_RETRY,
            failure_reason="unit_test",
        )

        with tempfile.TemporaryDirectory() as state_directory:
            spool_path = Path(state_directory) / "rejected.jsonl"
            with patch.object(
                pipeline_main, "REJECTED_FILE_PATH", str(spool_path)
            ), patch.object(
                pipeline_main,
                "analyze_with_ai",
                return_value=analysis_result,
            ) as analyze, patch.object(
                decoder, "decode_payload"
            ) as decode, patch("builtins.print"):
                pipeline_main.process_line(raw_record)

        analyze.assert_called_once_with(expected_payload)
        decode.assert_not_called()


if __name__ == "__main__":
    unittest.main()
