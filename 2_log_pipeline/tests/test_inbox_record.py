import base64
import hashlib
import json
import sys
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE_DIR))

from inbox_record import (
    InboxRecordError,
    ParseState,
    create_inbox_event,
    create_ingestion_key,
    inbox_event_filename,
    inbox_event_from_json,
    inbox_event_to_json,
)


class InboxRecordTests(unittest.TestCase):
    def make_event(self, raw_bytes=b'{"ok":true}\n', start=0, **kwargs):
        return create_inbox_event(
            "log-generation-1",
            start,
            start + len(raw_bytes),
            raw_bytes,
            ingested_at="2026-10-31T12:00:00+00:00",
            **kwargs,
        )

    def test_lf_and_crlf_records_preserve_delimiters_and_bytes(self):
        for raw_bytes in (b'{"ok":true}\n', b'{"ok":true}\r\n'):
            with self.subTest(raw_bytes=raw_bytes):
                event = self.make_event(raw_bytes)
                self.assertEqual(
                    base64.b64decode(event.raw_record_bytes_b64), raw_bytes
                )
                self.assertEqual(event.raw_record.encode("utf-8"), raw_bytes)

    def test_multibyte_unicode_and_newline_are_preserved_exactly(self):
        raw_bytes = '{"message":"Xin chào"}\n'.encode("utf-8")
        event = self.make_event(raw_bytes)

        self.assertEqual(event.raw_record, raw_bytes.decode("utf-8"))
        self.assertEqual(base64.b64decode(event.raw_record_bytes_b64), raw_bytes)
        self.assertEqual(event.byte_offset_end - event.byte_offset_start, len(raw_bytes))

    def test_invalid_utf8_is_quarantined_without_losing_raw_bytes(self):
        raw_bytes = b'{"message":"\xff"}\n'
        event = self.make_event(raw_bytes)

        self.assertIsNone(event.raw_record)
        self.assertEqual(event.parse_state, ParseState.QUARANTINED)
        self.assertIn("Invalid UTF-8", event.parse_error)
        self.assertEqual(base64.b64decode(event.raw_record_bytes_b64), raw_bytes)

    def test_valid_utf8_with_invalid_json_is_quarantined_and_preserved(self):
        raw_bytes = b'{"unfinished":\n'
        event = self.make_event(raw_bytes)

        self.assertEqual(event.raw_record, raw_bytes.decode("utf-8"))
        self.assertEqual(event.parse_state, ParseState.QUARANTINED)
        self.assertIn("Invalid JSON", event.parse_error)

    def test_valid_json_is_unvalidated_without_rewriting_source(self):
        raw_bytes = b' { "value" : 1 }\r\n'
        event = self.make_event(raw_bytes)

        self.assertEqual(event.parse_state, ParseState.UNVALIDATED)
        self.assertIsNone(event.parse_error)
        self.assertEqual(event.raw_record, raw_bytes.decode("utf-8"))

    def test_same_payload_at_different_ranges_has_different_keys(self):
        raw_bytes = b'{"same":true}\n'
        first = self.make_event(raw_bytes, start=0)
        second = self.make_event(raw_bytes, start=100)

        self.assertNotEqual(first.ingestion_key, second.ingestion_key)
        self.assertNotEqual(
            inbox_event_filename(first.ingestion_key),
            inbox_event_filename(second.ingestion_key),
        )

    def test_key_and_filename_are_stable_and_filename_is_sha256(self):
        key = create_ingestion_key("gen|part", 3, 10)

        self.assertEqual(key, create_ingestion_key("gen|part", 3, 10))
        self.assertEqual(
            inbox_event_filename(key),
            hashlib.sha256(key.encode("utf-8")).hexdigest() + ".event.json",
        )

    def test_generation_and_offset_validation(self):
        invalid_ranges = (
            ("", 0, 1),
            ("g", -1, 1),
            ("g", 0, 0),
            ("g", 2, 1),
            ("g", True, 2),
            ("g", 0, False),
            (None, 0, 1),
            ("g", 0.0, 1),
        )
        for generation, start, end in invalid_ranges:
            with self.subTest(generation=generation, start=start, end=end):
                with self.assertRaises(InboxRecordError):
                    create_ingestion_key(generation, start, end)

    def test_factory_rejects_mismatched_byte_range(self):
        with self.assertRaisesRegex(InboxRecordError, "range length"):
            create_inbox_event("g", 0, 2, b"x")

    def test_json_round_trip_preserves_event(self):
        event = self.make_event(
            '{"message":"Tiếng Việt"}\n'.encode("utf-8"),
            source_event_id="event-1",
        )

        self.assertEqual(inbox_event_from_json(inbox_event_to_json(event)), event)
        self.assertEqual(inbox_event_from_json(event.to_json()), event)

    def test_rejects_invalid_base64(self):
        data = json.loads(inbox_event_to_json(self.make_event()))
        data["raw_record_bytes_b64"] = "not-base64!"

        with self.assertRaisesRegex(InboxRecordError, "invalid"):
            inbox_event_from_json(json.dumps(data))

    def test_rejects_checksum_mismatch(self):
        data = json.loads(inbox_event_to_json(self.make_event()))
        data["raw_bytes_sha256"] = "0" * 64

        with self.assertRaisesRegex(InboxRecordError, "does not match"):
            inbox_event_from_json(json.dumps(data))

    def test_rejects_unsupported_version(self):
        data = json.loads(inbox_event_to_json(self.make_event()))
        data["version"] = 2

        with self.assertRaisesRegex(InboxRecordError, "version"):
            inbox_event_from_json(json.dumps(data))

    def test_rejects_raw_record_mismatch(self):
        data = json.loads(inbox_event_to_json(self.make_event()))
        data["raw_record"] = '{"changed":true}\n'

        with self.assertRaisesRegex(InboxRecordError, "raw_record"):
            inbox_event_from_json(json.dumps(data))

    def test_rejects_key_offset_and_duplicate_field_inconsistency(self):
        data = json.loads(inbox_event_to_json(self.make_event()))
        data["byte_offset_end"] += 1
        with self.assertRaises(InboxRecordError):
            inbox_event_from_json(json.dumps(data))

        serialized = inbox_event_to_json(self.make_event())
        duplicate = serialized[:-1] + ',"version":1}'
        with self.assertRaisesRegex(InboxRecordError, "Duplicate"):
            inbox_event_from_json(duplicate)

    def test_rejects_inconsistent_parse_state(self):
        data = json.loads(inbox_event_to_json(self.make_event()))
        data["parse_state"] = ParseState.QUARANTINED.value
        data["parse_error"] = "invented error"

        with self.assertRaisesRegex(InboxRecordError, "parse_state"):
            inbox_event_from_json(json.dumps(data))


if __name__ == "__main__":
    unittest.main()
