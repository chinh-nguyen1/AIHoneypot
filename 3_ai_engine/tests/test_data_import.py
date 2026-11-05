import json
import sys
import tempfile
import unittest
from pathlib import Path

AI_ENGINE_ROOT = Path(__file__).resolve().parents[1]
if str(AI_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_ENGINE_ROOT))

from data_import import (
    DatasetImporter,
    UnsupportedDatasetFormatError,
)


class DatasetImporterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def write(self, name, content):
        path = self.root / name
        path.write_text(content, encoding="utf-8", newline="")
        return path

    def read_all(self, path, **kwargs):
        importer = DatasetImporter(path, **kwargs)
        records = [
            record
            for batch in importer.iter_batches()
            for record in batch
        ]
        return records, importer.stats

    def test_imports_csv_header_records(self):
        path = self.write(
            "data.csv",
            "payload,label\n"
            '"SELECT * FROM users","SQLi"\n'
            '"<script>alert(1)</script>",XSS\n'
            "ordinary request,Normal\n",
        )

        records, stats = self.read_all(path, batch_size=2)

        self.assertEqual([record.label for record in records], ["SQLi", "XSS", "Normal"])
        self.assertTrue(all(record.source_file == str(path.resolve()) for record in records))
        self.assertEqual([record.source_record for record in records], [1, 2, 3])
        self.assertEqual(stats.records_read, 3)
        self.assertEqual(stats.valid_records, 3)
        self.assertEqual(stats.label_counts, {"SQLi": 1, "XSS": 1, "Normal": 1})

    def test_importer_canonicalizes_every_supported_label_alias(self):
        aliases = [
            ("normal", "Normal"),
            ("Normal", "Normal"),
            ("NORMAL", "Normal"),
            ("sqli", "SQLi"),
            ("SQLi", "SQLi"),
            ("sql_injection", "SQLi"),
            ("sql-injection", "SQLi"),
            ("sql injection", "SQLi"),
            ("xss", "XSS"),
            ("XSS", "XSS"),
            ("cross_site_scripting", "XSS"),
            ("cross-site-scripting", "XSS"),
            ("cross site scripting", "XSS"),
        ]
        path = self.write(
            "aliases.jsonl",
            "".join(
                json.dumps(
                    {"payload": f"payload-{index}", "label": alias}
                )
                + "\n"
                for index, (alias, _) in enumerate(aliases)
            ),
        )

        records, stats = self.read_all(path)

        self.assertEqual([record.label for record in records], [label for _, label in aliases])
        self.assertEqual(stats.valid_records, len(aliases))
        self.assertEqual(
            [(record.source_file, record.source_record) for record in records],
            [(str(path.resolve()), index) for index in range(1, len(aliases) + 1)],
        )

    def test_importer_trims_label_whitespace_but_preserves_payload(self):
        payload = "  SQLi%2F <SCRIPT>  "
        path = self.write(
            "trimmed-label.jsonl",
            json.dumps({"payload": payload, "label": " \tSQL-INJECTION \n"})
            + "\n",
        )

        records, _ = self.read_all(path)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].label, "SQLi")
        self.assertEqual(records[0].payload, payload)

    def test_importer_rejects_unknown_labels(self):
        path = self.write(
            "unknown-label.jsonl",
            '{"payload":"payload","label":"RCE"}\n',
        )

        records, stats = self.read_all(path)

        self.assertEqual(records, [])
        self.assertEqual(stats.rejected_records, 1)

    def test_imports_jsonl_and_rejects_missing_label(self):
        path = self.write(
            "data.jsonl",
            '{"payload":"query","label":"SQLi"}\n'
            '{"payload":"normal request"}\n',
        )

        records, stats = self.read_all(path)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].payload, "query")
        self.assertEqual(records[0].source_record, 1)
        self.assertEqual(stats.records_read, 2)
        self.assertEqual(stats.rejected_records, 1)

    def test_missing_payload_is_rejected(self):
        path = self.write(
            "missing-payload.jsonl",
            '{"label":"Normal"}\n',
        )

        records, stats = self.read_all(path)

        self.assertEqual(records, [])
        self.assertEqual(stats.records_read, 1)
        self.assertEqual(stats.rejected_records, 1)

    def test_txt_requires_label_tab_payload_format(self):
        path = self.write(
            "data.txt",
            "XSS\t<script>\n"
            "Normal\tordinary request\n"
            "unlabeled payload\n",
        )

        records, stats = self.read_all(path)

        self.assertEqual([record.label for record in records], ["XSS", "Normal"])
        self.assertEqual(stats.records_read, 3)
        self.assertEqual(stats.rejected_records, 1)

    def test_invalid_label_and_empty_payload_are_rejected(self):
        path = self.write(
            "data.jsonl",
            '{"payload":"unknown","label":"Benign"}\n'
            '{"payload":"  ","label":"Normal"}\n',
        )
        reject_path = self.root / "rejects.jsonl"

        records, stats = self.read_all(path, reject_report_path=reject_path)

        self.assertEqual(records, [])
        self.assertEqual(stats.records_read, 2)
        self.assertEqual(stats.rejected_records, 2)
        self.assertEqual(stats.empty_payloads, 1)
        rejected = [json.loads(line) for line in reject_path.read_text().splitlines()]
        self.assertEqual(len(rejected), 2)
        self.assertTrue(all("payload" not in item for item in rejected))
        self.assertIn("label", rejected[0]["reason"])

    def test_malformed_jsonl_and_non_object_records_are_rejected(self):
        path = self.write(
            "broken.jsonl",
            "{broken\n"
            '["not", "an", "object"]\n'
            '{"payload":"valid","label":"XSS"}',
        )

        records, stats = self.read_all(path)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].source_record, 3)
        self.assertEqual(stats.records_read, 3)
        self.assertEqual(stats.rejected_records, 2)

    def test_duplicate_payloads_are_counted_but_records_are_retained(self):
        path = self.write(
            "duplicates.csv",
            "payload,label\n"
            "same,SQLi\n"
            "same,SQLi\n"
            "same,XSS\n",
        )

        records, stats = self.read_all(path)

        self.assertEqual(len(records), 3)
        self.assertEqual(stats.duplicate_payloads, 2)
        self.assertEqual(stats.valid_records, 3)

    def test_empty_file_has_zero_counts(self):
        path = self.write("empty.jsonl", "")

        records, stats = self.read_all(path)

        self.assertEqual(records, [])
        self.assertEqual(stats.records_read, 0)
        self.assertEqual(stats.valid_records, 0)
        self.assertEqual(stats.rejected_records, 0)

    def test_large_simulated_jsonl_file_is_yielded_in_configured_batches(self):
        path = self.root / "many.jsonl"
        with path.open("w", encoding="utf-8") as stream:
            for index in range(25):
                stream.write(
                    json.dumps(
                        {"payload": f"payload-{index}", "label": "Normal"}
                    )
                    + "\n"
                )

        importer = DatasetImporter(path, batch_size=7)
        batch_sizes = [len(batch) for batch in importer.iter_batches()]

        self.assertEqual(batch_sizes, [7, 7, 7, 4])
        self.assertEqual(importer.stats.records_read, 25)
        self.assertEqual(importer.stats.valid_records, 25)

    def test_json_array_is_rejected_without_whole_file_json_loading(self):
        path = self.write("array.json", '[{"payload":"p","label":"Normal"}]')

        with self.assertRaisesRegex(
            UnsupportedDatasetFormatError, "streaming JSON parser"
        ):
            list(DatasetImporter(path).iter_batches())

    def test_csv_rows_with_wrong_field_count_are_rejected(self):
        path = self.write(
            "bad.csv",
            "payload,label\n"
            "valid,Normal\n"
            "too,many,fields\n",
        )

        records, stats = self.read_all(path)

        self.assertEqual(len(records), 1)
        self.assertEqual(stats.records_read, 2)
        self.assertEqual(stats.rejected_records, 1)

    def test_reject_report_does_not_overwrite_existing_file(self):
        path = self.write("data.jsonl", '{"payload":"","label":"Normal"}\n')
        report_path = self.write("rejects.jsonl", "keep this\n")

        with self.assertRaisesRegex(ValueError, "Reject report"):
            list(DatasetImporter(path, reject_report_path=report_path).iter_batches())

        self.assertEqual(report_path.read_text(encoding="utf-8"), "keep this\n")


if __name__ == "__main__":
    unittest.main()
