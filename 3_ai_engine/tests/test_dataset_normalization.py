import json
import sys
import tempfile
import unittest
import unicodedata
import weakref
from pathlib import Path

AI_ENGINE_ROOT = Path(__file__).resolve().parents[1]
if str(AI_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_ENGINE_ROOT))

from data_import import DatasetImporter, DatasetRecord, normalize_label
from dataset_normalization import (
    DatasetNormalizationError,
    DatasetNormalizer,
)


class DatasetNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def record(self, payload, label, source_record=1, source_file="source.csv"):
        return DatasetRecord(
            payload=payload,
            label=label,
            source_file=source_file,
            source_record=source_record,
        )

    def run_normalizer(self, records, **kwargs):
        normalizer = DatasetNormalizer(**kwargs)
        output = [
            record
            for output_batch in normalizer.iter_batches([records])
            for record in output_batch
        ]
        return output, normalizer.stats

    def test_canonical_labels_are_retained(self):
        records = [
            self.record("normal payload", "Normal"),
            self.record("sql payload", "SQLi", 2),
            self.record("xss payload", "XSS", 3),
        ]

        output, stats = self.run_normalizer(records)

        self.assertEqual([item.label for item in output], ["Normal", "SQLi", "XSS"])
        self.assertEqual(stats.counts_by_label, {"Normal": 1, "SQLi": 1, "XSS": 1})

    def test_declared_label_aliases_are_canonicalized(self):
        records = [
            self.record("p1", "normal"),
            self.record("p2", "NORMAL", 2),
            self.record("p3", "sqli", 3),
            self.record("p4", "sql_injection", 4),
            self.record("p5", "cross_site_scripting", 5),
        ]

        output, _ = self.run_normalizer(records)

        self.assertEqual(
            [item.label for item in output],
            ["Normal", "Normal", "SQLi", "SQLi", "XSS"],
        )

    def test_every_supported_alias_matches_importer_mapping(self):
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
        records = [
            self.record(f"payload-{index}", f"  {alias} \t", index + 1)
            for index, (alias, _) in enumerate(aliases)
        ]

        output, _ = self.run_normalizer(records)

        self.assertEqual([record.label for record in output], [label for _, label in aliases])
        self.assertEqual(
            [normalize_label(alias) for alias, _ in aliases],
            [label for _, label in aliases],
        )

    def test_unsupported_labels_are_rejected_without_falling_back_to_normal(self):
        records = [
            self.record("payload", "RCE"),
            self.record("payload2", "Path_Traversal", 2),
            self.record("payload3", "Suspicious", 3),
            self.record("payload4", "Benign", 4),
        ]

        output, stats = self.run_normalizer(records)

        self.assertEqual(output, [])
        self.assertEqual(stats.rejected_records, 4)
        self.assertEqual(
            stats.counts_by_reject_reason[
                "label is not one of Normal, SQLi, XSS or a declared alias"
            ],
            4,
        )

    def test_empty_and_whitespace_payloads_are_rejected(self):
        records = [
            self.record("", "Normal"),
            self.record(" \t\r\n ", "SQLi", 2),
        ]

        output, stats = self.run_normalizer(records)

        self.assertEqual(output, [])
        self.assertEqual(stats.rejected_records, 2)
        self.assertEqual(stats.counts_by_reject_reason["payload is empty"], 2)

    def test_missing_fields_and_wrong_record_types_are_rejected(self):
        records = [
            {"label": "Normal", "source_file": "src", "source_record": 1},
            {"payload": "payload", "source_file": "src", "source_record": 2},
            {"payload": 42, "label": "Normal", "source_file": "src", "source_record": 3},
            object(),
        ]

        output, stats = self.run_normalizer(records)

        self.assertEqual(output, [])
        self.assertEqual(stats.total_records, 4)
        self.assertEqual(stats.rejected_records, 4)

    def test_original_payload_is_preserved(self):
        original = "SELECT 1%2F2; <SCRIPT>alert('X')</SCRIPT>"
        output, _ = self.run_normalizer([self.record(original, "XSS")])

        self.assertEqual(output[0].payload, original)

    def test_nfc_equivalent_payloads_are_duplicates_without_lowercasing(self):
        composed = "caf\u00e9 <SCRIPT>!"
        decomposed = unicodedata.normalize("NFD", composed)
        output, stats = self.run_normalizer(
            [
                self.record(decomposed, "XSS"),
                self.record(composed, "XSS", 2),
                self.record("CAFÉ <SCRIPT>!", "XSS", 3),
            ]
        )

        self.assertEqual(len(output), 2)
        self.assertEqual(output[0].payload, decomposed)
        self.assertEqual(output[1].payload, "CAFÉ <SCRIPT>!")
        self.assertEqual(stats.duplicate_records, 1)

    def test_same_label_duplicate_keeps_first_record_and_source(self):
        output, stats = self.run_normalizer(
            [
                self.record("same", "sqli", 8, "first.csv"),
                self.record("same", "SQLi", 9, "second.csv"),
            ]
        )

        self.assertEqual(len(output), 1)
        self.assertEqual(output[0].label, "SQLi")
        self.assertEqual(output[0].source_file, "first.csv")
        self.assertEqual(output[0].source_record, 8)
        self.assertEqual(stats.duplicate_records, 1)
        self.assertEqual(stats.unique_records, 1)

    def test_cross_label_duplicate_is_excluded_and_reported_without_payload(self):
        payload = "PRIVATE_PAYLOAD_SHOULD_NOT_APPEAR"
        conflict_path = self.root / "conflicts.jsonl"
        output, stats = self.run_normalizer(
            [
                self.record(payload, "SQLi", 1, "a.csv"),
                self.record(payload, "XSS", 7, "b.csv"),
            ],
            conflict_report_path=conflict_path,
        )

        report = conflict_path.read_text(encoding="utf-8")
        rows = [json.loads(line) for line in report.splitlines()]
        self.assertEqual(output, [])
        self.assertEqual(stats.conflicting_records, 2)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["label"] for row in rows}, {"SQLi", "XSS"})
        self.assertEqual(
            {(row["source_file"], row["source_record"]) for row in rows},
            {("a.csv", 1), ("b.csv", 7)},
        )
        self.assertTrue(all("payload" not in row for row in rows))
        self.assertNotIn(payload, report)
        self.assertTrue(all(len(row["payload_fingerprint"]) == 64 for row in rows))

    def test_duplicate_detection_spans_distinct_input_batches(self):
        normalizer = DatasetNormalizer(batch_size=1)
        input_batches = iter(
            [
                [self.record("same", "Normal", 1)],
                [self.record("same", "normal", 2)],
                [self.record("different", "XSS", 3)],
            ]
        )

        output_batches = list(normalizer.iter_batches(input_batches))

        self.assertEqual([len(batch) for batch in output_batches], [1, 1])
        self.assertEqual(
            [record.payload for batch in output_batches for record in batch],
            ["same", "different"],
        )
        self.assertEqual(normalizer.stats.duplicate_records, 1)

    def test_later_label_conflict_removes_earlier_candidate_from_output(self):
        normalizer = DatasetNormalizer()
        output = [
            record
            for batch in normalizer.iter_batches(
                [
                    [self.record("conflicting payload", "Normal")],
                    [self.record("conflicting payload", "SQLi", 2)],
                ]
            )
            for record in batch
        ]

        self.assertEqual(output, [])
        self.assertEqual(normalizer.stats.conflicting_records, 2)
        self.assertEqual(normalizer.stats.unique_records, 0)

    def test_reject_report_contains_source_and_reason_but_not_payload(self):
        payload = "SENSITIVE_REJECTED_PAYLOAD"
        reject_path = self.root / "rejects.jsonl"
        output, stats = self.run_normalizer(
            [self.record(payload, "Unknown")],
            reject_report_path=reject_path,
        )

        report = reject_path.read_text(encoding="utf-8")
        row = json.loads(report)
        self.assertEqual(output, [])
        self.assertEqual(row["source_file"], "source.csv")
        self.assertEqual(row["source_record"], 1)
        self.assertIn("reason", row)
        self.assertNotIn(payload, report)
        self.assertEqual(stats.rejected_records, 1)

    def test_existing_reports_are_not_overwritten(self):
        reject_path = self.root / "rejects.jsonl"
        conflict_path = self.root / "conflicts.jsonl"
        reject_path.write_text("existing reject\n", encoding="utf-8")
        with self.assertRaises(DatasetNormalizationError):
            list(
                DatasetNormalizer(
                    reject_report_path=reject_path
                ).iter_batches([])
            )
        self.assertEqual(reject_path.read_text(encoding="utf-8"), "existing reject\n")

        conflict_path.write_text("existing conflict\n", encoding="utf-8")
        with self.assertRaises(DatasetNormalizationError):
            list(
                DatasetNormalizer(
                    conflict_report_path=conflict_path
                ).iter_batches([])
            )
        self.assertEqual(
            conflict_path.read_text(encoding="utf-8"), "existing conflict\n"
        )

    def test_output_is_batched_and_input_records_are_not_retained_in_memory(self):
        retained_references = []
        input_count = 300

        def imported_batches():
            for index in range(input_count):
                record = self.record(f"payload-{index}", "Normal", index + 1)
                retained_references.append(weakref.ref(record))
                yield [record]

        normalizer = DatasetNormalizer(batch_size=17)
        output_iterator = normalizer.iter_batches(imported_batches())
        first_output_batch = next(output_iterator)

        self.assertEqual(len(first_output_batch), 17)
        self.assertEqual(normalizer.stats.total_records, input_count)
        self.assertLessEqual(
            sum(reference() is not None for reference in retained_references), 2
        )

        output_batches = [first_output_batch, *output_iterator]
        self.assertEqual([len(batch) for batch in output_batches], [17] * 17 + [11])
        self.assertEqual(normalizer.stats.unique_records, input_count)

    def test_importer_batches_can_be_passed_directly(self):
        source_path = self.root / "imported.jsonl"
        source_path.write_text(
            '{"payload":"same","label":"SQLi"}\n'
            '{"payload":"same","label":"sqli"}\n'
            '{"payload":"other","label":"XSS"}\n',
            encoding="utf-8",
        )
        importer = DatasetImporter(source_path, batch_size=1)
        normalizer = DatasetNormalizer(batch_size=2)

        output = [
            record
            for batch in normalizer.iter_batches(importer.iter_batches())
            for record in batch
        ]

        self.assertEqual(
            [(record.payload, record.label) for record in output],
            [("same", "SQLi"), ("other", "XSS")],
        )
        self.assertEqual(normalizer.stats.total_records, 3)
        self.assertEqual(normalizer.stats.valid_records, 3)
        self.assertEqual(normalizer.stats.duplicate_records, 1)
        self.assertEqual(normalizer.stats.unique_records, 2)

    def test_stats_count_valid_rejected_duplicate_and_unique_records(self):
        output, stats = self.run_normalizer(
            [
                self.record("dupe", "Normal", 1),
                self.record("dupe", "Normal", 2),
                self.record("conflict", "SQLi", 3),
                self.record("conflict", "XSS", 4),
                self.record("rejected", "RCE", 5),
                self.record("unique", "Normal", 6),
            ]
        )

        self.assertEqual(stats.total_records, 6)
        self.assertEqual(stats.valid_records, 5)
        self.assertEqual(stats.rejected_records, 1)
        self.assertEqual(stats.duplicate_records, 2)
        self.assertEqual(stats.conflicting_records, 2)
        self.assertEqual(stats.unique_records, 2)
        self.assertEqual(stats.counts_by_label, {"Normal": 2, "SQLi": 0, "XSS": 0})
        self.assertEqual(
            stats.counts_by_reject_reason[
                "label is not one of Normal, SQLi, XSS or a declared alias"
            ],
            1,
        )


if __name__ == "__main__":
    unittest.main()
