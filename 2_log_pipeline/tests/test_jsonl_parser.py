import json
import sys
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE_DIR))

from jsonl_parser import JSONLParseError, NormalizedEvent, parse_jsonl_record
from normalized_event import NormalizedEvent as SharedNormalizedEvent


class JSONLParserTests(unittest.TestCase):
    def setUp(self):
        self.record = {
            "source_event_id": "0123456789abcdef0123456789abcdef",
            "timestamp": "2026-10-31T12:34:56+00:00",
            "source_ip": "192.0.2.10",
            "method": "POST",
            "uri": "/comment.php",
            "query_params": {"q": "áo dài"},
            "body_params": {"content": "nội dung"},
            "payload": {
                "query_params": {"q": "áo dài"},
                "body_params": {"content": "nội dung"},
            },
            "user_agent": "Test Client",
            "request_type": "web",
        }

    def encode_record(self, **updates):
        record = dict(self.record)
        record.update(updates)
        return json.dumps(record, ensure_ascii=False)

    def test_valid_record_returns_normalized_event_and_preserves_raw_record(self):
        raw_record = " \t" + self.encode_record() + "  "

        event = parse_jsonl_record(raw_record)

        self.assertIsInstance(event, NormalizedEvent)
        self.assertEqual(event.source_format, "jsonl")
        self.assertEqual(event.source_event_id, self.record["source_event_id"])
        self.assertEqual(event.raw_record, raw_record)
        self.assertIsNone(event.payload_decoded)
        self.assertEqual(event.present_fields, frozenset(self.record))
        self.assertIs(type(event), SharedNormalizedEvent)

    def test_normalized_event_imports_are_the_same_class(self):
        self.assertIs(NormalizedEvent, SharedNormalizedEvent)

    def test_preserves_vietnamese_unicode(self):
        event = parse_jsonl_record(self.encode_record())

        self.assertEqual(event.query_params, {"q": "áo dài"})
        self.assertEqual(event.body_params, {"content": "nội dung"})

    def test_preserves_quotes_pipe_and_special_characters(self):
        value = "payload 'quoted' \"double\" | <>&=%"
        event = parse_jsonl_record(
            self.encode_record(
                body_params={"content": value},
                payload={"query_params": {"q": "áo dài"}, "body_params": {"content": value}},
            )
        )

        self.assertEqual(event.body_params["content"], value)
        self.assertEqual(event.payload_raw["body_params"]["content"], value)

    def test_preserves_escaped_newline(self):
        value = "line one\nline two"
        raw_record = self.encode_record(
            body_params={"content": value},
            payload={"query_params": {"q": "áo dài"}, "body_params": {"content": value}},
        )

        event = parse_jsonl_record(raw_record)

        self.assertIn("\\n", raw_record)
        self.assertEqual(event.payload_raw["body_params"]["content"], value)
        self.assertEqual(event.raw_record, raw_record)

    def test_params_accept_objects(self):
        event = parse_jsonl_record(self.encode_record())

        self.assertEqual(event.query_params, {"q": "áo dài"})
        self.assertEqual(event.body_params, {"content": "nội dung"})

    def test_params_accept_empty_arrays(self):
        event = parse_jsonl_record(
            self.encode_record(
                query_params=[],
                body_params=[],
                payload={"query_params": [], "body_params": []},
            )
        )

        self.assertEqual(event.query_params, [])
        self.assertEqual(event.body_params, [])
        self.assertEqual(event.payload_raw, {"query_params": [], "body_params": []})

    def test_rejects_malformed_json(self):
        with self.assertRaisesRegex(JSONLParseError, "Invalid JSON record"):
            parse_jsonl_record('{"source_event_id":')

    def test_rejects_json_array_root(self):
        with self.assertRaisesRegex(JSONLParseError, "must be an object"):
            parse_jsonl_record("[]")

    def test_rejects_missing_source_event_id(self):
        record = dict(self.record)
        del record["source_event_id"]

        with self.assertRaisesRegex(JSONLParseError, "source_event_id"):
            parse_jsonl_record(json.dumps(record))

    def test_rejects_source_event_id_wrong_type_or_format(self):
        for value in (123, "", "not-an-event-id"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(JSONLParseError, "source_event_id"):
                    parse_jsonl_record(self.encode_record(source_event_id=value))

    def test_rejects_timestamp_wrong_type(self):
        with self.assertRaisesRegex(JSONLParseError, "timestamp.*string"):
            parse_jsonl_record(self.encode_record(timestamp=123))

    def test_accepts_utc_timestamp_and_preserves_original_value(self):
        timestamp = "2026-10-31T12:34:56Z"

        event = parse_jsonl_record(self.encode_record(timestamp=timestamp))

        self.assertEqual(event.timestamp, timestamp)

    def test_accepts_offset_timestamp_and_preserves_original_value(self):
        timestamp = "2026-10-31T19:34:56+07:00"

        event = parse_jsonl_record(self.encode_record(timestamp=timestamp))

        self.assertEqual(event.timestamp, timestamp)

    def test_rejects_invalid_timestamp(self):
        with self.assertRaisesRegex(JSONLParseError, "valid ISO 8601"):
            parse_jsonl_record(self.encode_record(timestamp="not-a-timestamp+00:00"))

    def test_rejects_timestamp_without_timezone(self):
        with self.assertRaisesRegex(JSONLParseError, "must include a timezone"):
            parse_jsonl_record(self.encode_record(timestamp="2026-10-31T12:34:56"))

    def test_rejects_mismatched_outer_query_params(self):
        with self.assertRaisesRegex(
            JSONLParseError, r"query_params.*payload\.query_params"
        ):
            parse_jsonl_record(self.encode_record(query_params={"q": "different"}))

    def test_rejects_mismatched_outer_body_params(self):
        with self.assertRaisesRegex(
            JSONLParseError, r"body_params.*payload\.body_params"
        ):
            parse_jsonl_record(self.encode_record(body_params={"content": "different"}))

    def test_rejects_similar_values_with_different_json_types(self):
        cases = (
            (
                {"query_params": {"0": "value"}, "payload": {"query_params": ["value"], "body_params": {"content": "nội dung"}}},
                r"query_params.*does not match",
            ),
            (
                {"query_params": {"flag": True}, "payload": {"query_params": {"flag": 1}, "body_params": {"content": "nội dung"}}},
                r"query_params.*does not match",
            ),
        )
        for updates, message in cases:
            with self.subTest(updates=updates):
                with self.assertRaisesRegex(JSONLParseError, message):
                    parse_jsonl_record(self.encode_record(**updates))

    def test_nullable_fields_accept_null(self):
        raw_record = self.encode_record(source_ip=None, user_agent=None)
        event = parse_jsonl_record(raw_record)

        self.assertIsNone(event.source_ip)
        self.assertIsNone(event.user_agent)
        self.assertIn("source_ip", event.present_fields)
        self.assertIn("user_agent", event.present_fields)
        self.assertEqual(event.raw_record, raw_record)

    def test_rejects_missing_required_field(self):
        record = dict(self.record)
        del record["method"]

        with self.assertRaisesRegex(JSONLParseError, "method"):
            parse_jsonl_record(json.dumps(record))

    def test_rejects_missing_or_wrong_type_payload(self):
        for payload in (None, [], "payload"):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(JSONLParseError, "payload"):
                    parse_jsonl_record(self.encode_record(payload=payload))

    def test_rejects_payload_missing_params(self):
        with self.assertRaisesRegex(JSONLParseError, "payload.*body_params"):
            parse_jsonl_record(
                self.encode_record(payload={"query_params": {"q": "test"}})
            )

    def test_rejects_params_with_invalid_type(self):
        with self.assertRaisesRegex(JSONLParseError, "query_params"):
            parse_jsonl_record(self.encode_record(query_params="q=test"))

    def test_rejects_non_standard_json_constants(self):
        with self.assertRaisesRegex(JSONLParseError, "Invalid JSON record"):
            parse_jsonl_record(self.encode_record().replace('"timestamp":', '"extra": NaN, "timestamp":'))


if __name__ == "__main__":
    unittest.main()
