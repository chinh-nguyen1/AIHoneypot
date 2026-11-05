import sys
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE_DIR))

from normalized_event import NormalizedEvent


class NormalizedEventTests(unittest.TestCase):
    def make_event(self, **values):
        return NormalizedEvent(source_format="legacy", raw_record="raw", **values)

    def test_present_fields_distinguishes_missing_null_and_empty(self):
        missing = self.make_event()
        null_value = self.make_event(
            source_ip=None, present_fields=frozenset({"source_ip"})
        )
        empty_value = self.make_event(
            query_params=[],
            present_fields=frozenset({"query_params"}),
        )

        self.assertNotIn("source_ip", missing.present_fields)
        self.assertIsNone(missing.source_ip)
        self.assertIn("source_ip", null_value.present_fields)
        self.assertIsNone(null_value.source_ip)
        self.assertIn("query_params", empty_value.present_fields)
        self.assertEqual(empty_value.query_params, [])

    def test_payload_raw_accepts_json_value_string_and_none_without_coercion(self):
        values = (
            {"key": "value"},
            ["value"],
            12,
            True,
            "raw payload",
            None,
        )

        for value in values:
            with self.subTest(value=value):
                event = self.make_event(payload_raw=value)
                self.assertIs(event.payload_raw, value)

    def test_payload_decoded_defaults_to_none(self):
        event = self.make_event()

        self.assertIsNone(event.payload_decoded)

    def test_raw_record_is_preserved(self):
        raw_record = '  {"payload":"line one\\nline two"}  '
        event = NormalizedEvent(source_format="jsonl", raw_record=raw_record)

        self.assertEqual(event.raw_record, raw_record)

    def test_present_fields_defaults_to_empty_frozenset(self):
        event = self.make_event()

        self.assertEqual(event.present_fields, frozenset())


if __name__ == "__main__":
    unittest.main()
