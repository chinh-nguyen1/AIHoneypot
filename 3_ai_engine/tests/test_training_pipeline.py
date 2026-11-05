import json
import multiprocessing
import sqlite3
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

import numpy as np
from sklearn.ensemble import RandomForestClassifier

AI_ENGINE_ROOT = Path(__file__).resolve().parents[1]
if str(AI_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_ENGINE_ROOT))

from data_import import DatasetRecord
from dataset_normalization import DatasetNormalizer, NormalizedDatasetRecord
import training_pipeline
from training_pipeline import (
    LABELS,
    TrainingConfig,
    TrainingPipelineError,
    _assign_splits,
    _comparison,
    _create_staging_schema,
    _metrics_from_confusion,
    _publish_release,
    _rename_directory_noreplace,
    _stage_and_close_iterators,
    _stage_normalized_batches,
    _train_baseline,
    _train_incremental_model,
    _iter_split_batches,
    run_training_pipeline,
)


def _publish_from_process(release_root, version, identity, ready, start, results):
    ready.put(True)
    if not start.wait(15):
        results.put(("error", "timed out waiting to start"))
        return
    try:
        _publish_release(Path(release_root), version, {}, {"process": identity})
        results.put(("published", identity))
    except FileExistsError:
        results.put(("collision", identity))
    except BaseException as exc:
        results.put(("error", f"{type(exc).__name__}: {exc}"))


class TrainingPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def records(self, per_class=12):
        for label in LABELS:
            for index in range(per_class):
                yield DatasetRecord(
                    payload=f"{label} request payload {index}",
                    label=label,
                    source_file=f"{label.lower()}.jsonl",
                    source_record=index + 1,
                )

    def write_dataset(self, per_class=12, extra_records=()):
        path = self.root / "dataset.jsonl"
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            aliases = {
                "Normal": "normal",
                "SQLi": "sql_injection",
                "XSS": "cross_site_scripting",
            }
            for label in LABELS:
                for index in range(per_class):
                    stream.write(
                        json.dumps(
                            {
                                "payload": f"{label} request payload {index}",
                                "label": aliases[label],
                            }
                        )
                        + "\n"
                    )
            for record in extra_records:
                stream.write(json.dumps(record) + "\n")
        return path

    def config(self, **overrides):
        values = {
            "batch_size": 4,
            "n_features": 512,
            "baseline_max_samples": 1000,
            "baseline_max_total_payload_chars": 5_000_000,
            "baseline_max_payload_chars": 100_000,
            "small_class_warning_threshold": 20,
        }
        values.update(overrides)
        return TrainingConfig(**values)

    def make_database(self, records):
        database = sqlite3.connect(":memory:")
        _create_staging_schema(database)
        _stage_normalized_batches(database, [records])
        return database

    def normalized_records(self, records):
        normalizer = DatasetNormalizer(batch_size=3)
        return normalizer, normalizer.iter_batches([records])

    def assignments(self, records, seed):
        database = self.make_database(records)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                counts = _assign_splits(database, seed)
            assignments = dict(
                database.execute(
                    "SELECT fingerprint, split FROM records ORDER BY fingerprint"
                )
            )
            return counts, assignments
        finally:
            database.close()

    def test_importer_normalizer_integration_canonical_labels_and_provenance(self):
        path = self.write_dataset(
            per_class=12,
            extra_records=[{"payload": "ignored attack", "label": "RCE"}],
        )
        releases = self.root / "releases"
        temporary = self.root / "temporary"
        temporary.mkdir()

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            result = run_training_pipeline(
                path,
                release_root=releases,
                temp_root=temporary,
                config=self.config(baseline_max_samples=1),
                version="integration-test",
            )

        self.assertEqual(result.metadata["import_stats"]["rejected_records"], 1)
        self.assertEqual(
            result.metadata["normalization_stats"]["counts_by_label"],
            {"SQLi": 12, "XSS": 12, "Normal": 12},
        )
        self.assertEqual(
            result.metadata["dataset_provenance"][0]["source_file"],
            str(path.resolve()),
        )
        self.assertEqual(result.metadata["candidates"]["alternative"]["status"], "completed")
        self.assertEqual(result.metadata["candidates"]["baseline"]["status"], "skipped")
        self.assertEqual(
            result.metadata["candidates"]["baseline"]["failure"]["code"],
            "sample_count_limit_exceeded",
        )
        self.assertEqual(list(temporary.iterdir()), [])
        self.assertTrue((result.release_path / "metadata.json").is_file())
        self.assertTrue((result.release_path / "alternative" / "model.joblib").is_file())
        self.assertTrue(
            (
                result.release_path
                / "alternative"
                / "feature_transform.joblib"
            ).is_file()
        )

    def test_normalizer_excludes_duplicate_payload_conflicts(self):
        source = list(self.records(4))
        source.extend(
            [
                DatasetRecord("conflicting payload", "Normal", "a.jsonl", 1),
                DatasetRecord("conflicting payload", "SQLi", "b.jsonl", 1),
            ]
        )
        normalizer, output_batches = self.normalized_records(source)
        output = [record for batch in output_batches for record in batch]
        self.assertEqual(normalizer.stats.conflicting_records, 2)
        self.assertEqual(normalizer.stats.unique_records, len(source) - 2)
        self.assertNotIn("conflicting payload", {record.payload for record in output})

    def test_duplicate_normalized_payload_is_only_assigned_to_one_split(self):
        source = list(self.records(4))
        source.extend(
            [
                DatasetRecord("cafe\u0301", "Normal", "first.jsonl", 100),
                DatasetRecord("caf\u00e9", "Normal", "second.jsonl", 101),
            ]
        )
        normalizer, output_batches = self.normalized_records(source)
        database = sqlite3.connect(":memory:")
        try:
            _create_staging_schema(database)
            unique_count = _stage_normalized_batches(database, output_batches)
            self.assertEqual(normalizer.stats.duplicate_records, 1)
            self.assertEqual(unique_count, len(source) - 1)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                _assign_splits(database, 42)
            matching = database.execute(
                "SELECT COUNT(DISTINCT split), COUNT(*) FROM records "
                "WHERE normalized_payload = ?",
                ("caf\u00e9",),
            ).fetchone()
            self.assertEqual(matching, (1, 1))
            stored = database.execute(
                "SELECT payload, source_file, source_record FROM records "
                "WHERE normalized_payload = ?",
                ("caf\u00e9",),
            ).fetchone()
            self.assertIn(stored[0], ("cafe\u0301", "caf\u00e9"))
            self.assertIn(stored[1], ("first.jsonl", "second.jsonl"))
            self.assertIn(stored[2], (100, 101))
        finally:
            database.close()

    def test_same_seed_has_same_assignments_and_different_seed_can_change_them(self):
        records = list(self.records(30))
        _, first = self.assignments(records, 42)
        _, repeated = self.assignments(records, 42)
        _, changed = self.assignments(records, 17)
        self.assertEqual(first, repeated)
        self.assertNotEqual(first, changed)

    def test_stratified_counts_preserve_all_samples_and_approximate_ratio(self):
        counts, assignments = self.assignments(list(self.records(20)), 42)
        self.assertEqual(len(assignments), 60)
        for label in LABELS:
            self.assertEqual(
                [counts[split][label] for split in ("train", "validation", "test")],
                [14, 3, 3],
            )
        self.assertEqual(sum(sum(row.values()) for row in counts.values()), 60)

    def test_split_fails_when_class_cannot_be_represented_everywhere(self):
        database = self.make_database(list(self.records(2)))
        try:
            with self.assertRaisesRegex(TrainingPipelineError, "at least 3"):
                _assign_splits(database, 42)
        finally:
            database.close()

    def test_sgd_consumes_training_records_in_bounded_batches(self):
        database = self.make_database(list(self.records(12)))
        observed_sizes = []
        original_batch_iterator = training_pipeline._iter_split_batches
        original_vectorizer = training_pipeline.HashingVectorizer

        def tracked_batches(database_arg, split, batch_size):
            for payloads, labels in original_batch_iterator(
                database_arg, split, batch_size
            ):
                observed_sizes.append((split, len(payloads)))
                yield payloads, labels

        class TrackingVectorizer(original_vectorizer):
            def transform(self, raw_documents):
                observed_sizes.append(("transform", len(raw_documents)))
                return super().transform(raw_documents)

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                _assign_splits(database, 42)
            train_count = database.execute(
                "SELECT COUNT(*) FROM records WHERE split = 'train'"
            ).fetchone()[0]
            config = self.config(batch_size=5)
            with mock.patch(
                "training_pipeline._iter_split_batches",
                side_effect=tracked_batches,
            ), mock.patch(
                "training_pipeline.HashingVectorizer",
                TrackingVectorizer,
            ):
                _train_incremental_model(database, config)

            training_batches = [
                size for split, size in observed_sizes if split == "train"
            ]
            transform_sizes = [
                size for split, size in observed_sizes if split == "transform"
            ]
            self.assertEqual(sum(training_batches), train_count)
            self.assertTrue(training_batches)
            self.assertLessEqual(max(training_batches), config.batch_size)
            self.assertTrue(all(size <= config.batch_size for size in transform_sizes))
            self.assertEqual(
                len(training_batches),
                (train_count + config.batch_size - 1) // config.batch_size,
            )
        finally:
            database.close()

    def test_sgd_initializes_all_classes_and_supports_inference_contract(self):
        database = self.make_database(list(self.records(6)))
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                _assign_splits(database, 42)
            vectorizer, model, _ = _train_incremental_model(
                database, self.config(batch_size=3)
            )
            self.assertEqual(set(model.classes_), set(LABELS))
            payloads, _ = next(_iter_split_batches(database, "test", 2))
            features = vectorizer.transform(payloads)
            self.assertEqual(len(model.predict(features)), len(payloads))
            self.assertEqual(model.predict_proba(features).shape, (len(payloads), 3))
        finally:
            database.close()

    def test_baseline_safety_limit_skips_without_training(self):
        database = self.make_database(list(self.records(6)))
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                _assign_splits(database, 42)
            result = _train_baseline(
                database,
                self.config(baseline_max_samples=1),
            )
            self.assertIsNone(result[0])
            self.assertIsNone(result[1])
            self.assertEqual(result[3]["code"], "sample_count_limit_exceeded")
        finally:
            database.close()

    def test_baseline_character_limits_are_checked_before_tfidf_creation(self):
        database = self.make_database(list(self.records(6)))
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                _assign_splits(database, 42)

            cases = (
                (
                    self.config(baseline_max_total_payload_chars=1),
                    "total_payload_characters_limit_exceeded",
                ),
                (
                    self.config(baseline_max_payload_chars=1),
                    "individual_payload_limit_exceeded",
                ),
            )
            for config, expected_code in cases:
                with self.subTest(expected_code=expected_code), mock.patch(
                    "training_pipeline.TfidfVectorizer"
                ) as vectorizer:
                    result = _train_baseline(database, config)
                    self.assertIsNone(result[0])
                    self.assertIsNone(result[1])
                    self.assertEqual(result[3]["code"], expected_code)
                    vectorizer.assert_not_called()
        finally:
            database.close()

    def test_baseline_sample_limit_is_checked_before_tfidf_creation(self):
        database = self.make_database(list(self.records(6)))
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                _assign_splits(database, 42)
            with mock.patch("training_pipeline.TfidfVectorizer") as vectorizer:
                result = _train_baseline(
                    database,
                    self.config(baseline_max_samples=1),
                )
            self.assertEqual(result[3]["code"], "sample_count_limit_exceeded")
            vectorizer.assert_not_called()
        finally:
            database.close()

    def test_baseline_does_not_hide_unexpected_programming_errors(self):
        database = self.make_database(list(self.records(6)))
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                _assign_splits(database, 42)
            with mock.patch(
                "training_pipeline.TfidfVectorizer",
                side_effect=RuntimeError("unexpected defect"),
            ):
                with self.assertRaisesRegex(RuntimeError, "unexpected defect"):
                    _train_baseline(database, self.config())
        finally:
            database.close()

    def test_baseline_evaluation_resource_failure_preserves_sgd_release(self):
        path = self.write_dataset(per_class=12)
        original_evaluate = training_pipeline._evaluate_model

        def fail_for_baseline(database, split, vectorizer, model, batch_size):
            if isinstance(model, RandomForestClassifier) and split == "validation":
                raise MemoryError("baseline evaluation resource failure")
            return original_evaluate(database, split, vectorizer, model, batch_size)

        release_root = self.root / "baseline-evaluation-failure"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            with mock.patch(
                "training_pipeline._evaluate_model",
                side_effect=fail_for_baseline,
            ):
                result = run_training_pipeline(
                    path,
                    release_root=release_root,
                    config=self.config(),
                    version="alternative-survives",
                )

        self.assertEqual(result.metadata["candidates"]["alternative"]["status"], "completed")
        self.assertEqual(result.metadata["candidates"]["baseline"]["status"], "failed")
        self.assertEqual(
            result.metadata["candidates"]["baseline"]["failure"]["stage"],
            "validation_evaluation",
        )
        self.assertTrue(
            (result.release_path / "alternative" / "model.joblib").is_file()
        )
        self.assertFalse((result.release_path / "baseline").exists())

    def test_baseline_sqlite_extraction_failure_preserves_sgd_candidate(self):
        path = self.write_dataset(per_class=12)
        original_batches = training_pipeline._iter_split_batches
        original_train_baseline = training_pipeline._train_baseline
        baseline_started = False

        def fail_during_baseline_extraction(database, split, batch_size):
            if baseline_started and split == "train":
                raise sqlite3.OperationalError("staging database I/O failed")
            yield from original_batches(database, split, batch_size)

        def start_baseline_extraction(database, config):
            nonlocal baseline_started
            baseline_started = True
            return original_train_baseline(database, config)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            with mock.patch.multiple(
                "training_pipeline",
                _iter_split_batches=mock.DEFAULT,
                _train_baseline=mock.DEFAULT,
            ) as patched:
                patched["_iter_split_batches"].side_effect = (
                    fail_during_baseline_extraction
                )
                patched["_train_baseline"].side_effect = start_baseline_extraction
                result = run_training_pipeline(
                    path,
                    release_root=self.root / "sqlite-extraction-failure",
                    config=self.config(),
                    version="sqlite-failure-keeps-sgd",
                )

        self.assertEqual(
            result.metadata["candidates"]["alternative"]["status"],
            "completed",
        )
        self.assertEqual(
            result.metadata["candidates"]["baseline"]["status"],
            "failed",
        )
        self.assertEqual(
            result.metadata["candidates"]["baseline"]["failure"]["code"],
            "operational_error",
        )
        self.assertEqual(
            result.metadata["candidates"]["baseline"]["failure"]["stage"],
            "data_extraction",
        )
        self.assertTrue(
            (result.release_path / "alternative" / "model.joblib").is_file()
        )
        self.assertFalse((result.release_path / "baseline").exists())

    def test_tfidf_resource_failure_preserves_sgd_and_excludes_baseline(self):
        path = self.write_dataset(per_class=12)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            with mock.patch(
                "training_pipeline.TfidfVectorizer.fit_transform",
                side_effect=MemoryError("TF-IDF allocation failed"),
            ):
                result = run_training_pipeline(
                    path,
                    release_root=self.root / "tfidf-resource-failure",
                    config=self.config(),
                    version="tfidf-failure-keeps-sgd",
                )

        self.assertEqual(result.metadata["candidates"]["alternative"]["status"], "completed")
        self.assertEqual(result.metadata["candidates"]["baseline"]["status"], "failed")
        self.assertEqual(
            result.metadata["candidates"]["baseline"]["failure"]["stage"],
            "tfidf_fit_transform",
        )
        self.assertTrue(
            (result.release_path / "alternative" / "model.joblib").is_file()
        )
        self.assertFalse((result.release_path / "baseline").exists())

    def test_random_forest_fit_resource_failure_preserves_sgd(self):
        path = self.write_dataset(per_class=12)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            with mock.patch(
                "training_pipeline.RandomForestClassifier.fit",
                side_effect=MemoryError("forest allocation failed"),
            ):
                result = run_training_pipeline(
                    path,
                    release_root=self.root / "forest-resource-failure",
                    config=self.config(),
                    version="forest-failure-keeps-sgd",
                )

        self.assertEqual(result.metadata["candidates"]["alternative"]["status"], "completed")
        self.assertEqual(result.metadata["candidates"]["baseline"]["status"], "failed")
        self.assertEqual(
            result.metadata["candidates"]["baseline"]["failure"]["stage"],
            "random_forest_fit",
        )
        self.assertFalse((result.release_path / "baseline").exists())

    def test_baseline_contract_memory_failure_preserves_sgd(self):
        path = self.write_dataset(per_class=12)
        original_verify = training_pipeline._verify_prediction_contract

        def fail_baseline_contract(vectorizer, model, database):
            if isinstance(model, RandomForestClassifier):
                raise MemoryError("contract verification ran out of memory")
            return original_verify(vectorizer, model, database)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            with mock.patch(
                "training_pipeline._verify_prediction_contract",
                side_effect=fail_baseline_contract,
            ):
                result = run_training_pipeline(
                    path,
                    release_root=self.root / "contract-memory-failure",
                    config=self.config(),
                    version="contract-failure-keeps-sgd",
                )

        self.assertEqual(result.metadata["candidates"]["alternative"]["status"], "completed")
        self.assertEqual(result.metadata["candidates"]["baseline"]["status"], "failed")
        self.assertEqual(
            result.metadata["candidates"]["baseline"]["failure"]["stage"],
            "prediction_contract",
        )
        self.assertFalse((result.release_path / "baseline").exists())

    def test_baseline_test_evaluation_failure_preserves_sgd_final_metrics(self):
        path = self.write_dataset(per_class=12)
        original_evaluate = training_pipeline._evaluate_model

        def fail_test_for_baseline(database, split, vectorizer, model, batch_size):
            if isinstance(model, RandomForestClassifier) and split == "test":
                raise sqlite3.OperationalError("test split read failed")
            return original_evaluate(database, split, vectorizer, model, batch_size)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            with mock.patch(
                "training_pipeline._evaluate_model",
                side_effect=fail_test_for_baseline,
            ):
                result = run_training_pipeline(
                    path,
                    release_root=self.root / "test-evaluation-failure",
                    config=self.config(),
                    version="test-failure-keeps-sgd",
                )

        self.assertEqual(result.metadata["candidates"]["alternative"]["status"], "completed")
        self.assertEqual(result.metadata["candidates"]["baseline"]["status"], "failed")
        self.assertEqual(
            result.metadata["candidates"]["baseline"]["failure"]["stage"],
            "test_evaluation",
        )
        self.assertIn("test", result.metadata["candidates"]["alternative"]["metrics"])
        self.assertFalse((result.release_path / "baseline").exists())

    def test_baseline_contract_failure_preserves_sgd_candidate(self):
        path = self.write_dataset(per_class=12)
        original_verify = training_pipeline._verify_prediction_contract

        def fail_for_baseline(vectorizer, model, database):
            if isinstance(model, RandomForestClassifier):
                raise training_pipeline._ModelContractError(
                    "baseline contract check failed"
                )
            return original_verify(vectorizer, model, database)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            with mock.patch(
                "training_pipeline._verify_prediction_contract",
                side_effect=fail_for_baseline,
            ):
                result = run_training_pipeline(
                    path,
                    release_root=self.root / "baseline-contract-failure",
                    config=self.config(),
                    version="alternative-contract-survives",
                )

        self.assertEqual(result.metadata["candidates"]["alternative"]["status"], "completed")
        self.assertEqual(result.metadata["candidates"]["baseline"]["status"], "failed")
        self.assertEqual(
            result.metadata["candidates"]["baseline"]["failure"]["code"],
            "model_contract_failure",
        )
        self.assertTrue(
            (result.release_path / "alternative" / "model.joblib").is_file()
        )
        self.assertFalse((result.release_path / "baseline").exists())

    def test_small_class_warning_uses_configured_threshold(self):
        records = list(self.records(6))
        database = self.make_database(records)
        try:
            with warnings.catch_warnings(record=True) as captured:
                warnings.simplefilter("always")
                _assign_splits(database, 42, small_class_warning_threshold=7)
            self.assertEqual(len(captured), len(LABELS))
            self.assertTrue(all("only 6 eligible unique samples" in str(w.message) for w in captured))

            database.execute("UPDATE records SET split = NULL, split_key = NULL")
            database.commit()
            with warnings.catch_warnings(record=True) as captured:
                warnings.simplefilter("always")
                _assign_splits(database, 42, small_class_warning_threshold=5)
            self.assertEqual(captured, [])
            self.assertEqual(
                database.execute(
                    "SELECT COUNT(*) FROM records WHERE split IS NOT NULL"
                ).fetchone()[0],
                len(records),
            )
        finally:
            database.close()

    def test_importer_and_normalizer_iterators_close_after_successful_staging(self):
        class CloseableIterator:
            def __init__(self, values):
                self._values = iter(values)
                self.closed = False

            def __iter__(self):
                return self

            def __next__(self):
                return next(self._values)

            def close(self):
                self.closed = True

        importer_batches = CloseableIterator([])
        normalized_batches = CloseableIterator(
            [[
                NormalizedDatasetRecord(
                    "safe payload",
                    "Normal",
                    "source.jsonl",
                    1,
                )
            ]]
        )
        database = sqlite3.connect(":memory:")
        try:
            _create_staging_schema(database)
            self.assertEqual(
                _stage_and_close_iterators(
                    database,
                    importer_batches,
                    normalized_batches,
                ),
                1,
            )
            self.assertTrue(importer_batches.closed)
            self.assertTrue(normalized_batches.closed)
        finally:
            database.close()

    def test_iterator_cleanup_errors_do_not_hide_staging_error(self):
        class CloseableIterator:
            def __init__(self, values):
                self._values = iter(values)
                self.closed = False

            def __iter__(self):
                return self

            def __next__(self):
                return next(self._values)

            def close(self):
                self.closed = True
                raise OSError("close failed")

        importer_batches = CloseableIterator([])
        normalized_batches = CloseableIterator(
            [[NormalizedDatasetRecord(None, "Normal", "source.jsonl", 1)]]
        )
        database = sqlite3.connect(":memory:")
        try:
            _create_staging_schema(database)
            with self.assertRaisesRegex(
                TrainingPipelineError, "missing or empty payload"
            ) as raised:
                _stage_and_close_iterators(
                    database,
                    importer_batches,
                    normalized_batches,
                )
            self.assertTrue(importer_batches.closed)
            self.assertTrue(normalized_batches.closed)
            if hasattr(raised.exception, "__notes__"):
                self.assertTrue(
                    any("close failed" in note for note in raised.exception.__notes__)
                )
        finally:
            database.close()

    def test_normalizer_exception_closes_both_generators(self):
        closed = {"importer": False, "normalizer": False}

        def importer_generator():
            try:
                yield []
            finally:
                closed["importer"] = True

        def normalizer_generator():
            try:
                yield []
                raise ValueError("normalization failed")
            finally:
                closed["normalizer"] = True

        importer_batches = importer_generator()
        normalized_batches = normalizer_generator()
        next(importer_batches)
        database = sqlite3.connect(":memory:")
        try:
            _create_staging_schema(database)
            with self.assertRaisesRegex(ValueError, "normalization failed"):
                _stage_and_close_iterators(
                    database,
                    importer_batches,
                    normalized_batches,
                )
            self.assertEqual(closed, {"importer": True, "normalizer": True})
        finally:
            database.close()

    def test_importer_iteration_exception_closes_both_generators(self):
        closed = {"importer": False, "normalizer": False}

        def importer_generator():
            try:
                yield []
                raise OSError("importer read failed")
            finally:
                closed["importer"] = True

        importer_batches = importer_generator()

        def normalizer_generator():
            try:
                for batch in importer_batches:
                    yield batch
            finally:
                closed["normalizer"] = True

        normalized_batches = normalizer_generator()
        database = sqlite3.connect(":memory:")
        try:
            _create_staging_schema(database)
            with self.assertRaisesRegex(OSError, "importer read failed"):
                _stage_and_close_iterators(
                    database,
                    importer_batches,
                    normalized_batches,
                )
            self.assertEqual(closed, {"importer": True, "normalizer": True})
        finally:
            database.close()

    def test_baseline_and_alternative_share_split_assignments(self):
        database = self.make_database(list(self.records(6)))
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                _assign_splits(database, 42)
            before = dict(
                database.execute(
                    "SELECT fingerprint, split FROM records ORDER BY fingerprint"
                )
            )
            alternative = _train_incremental_model(database, self.config())[1]
            baseline = _train_baseline(database, self.config())
            after = dict(
                database.execute(
                    "SELECT fingerprint, split FROM records ORDER BY fingerprint"
                )
            )
            self.assertIsNotNone(alternative)
            self.assertIsNone(baseline[3])
            self.assertEqual(before, after)
        finally:
            database.close()

    def test_test_split_is_final_evaluation_only_not_threshold_or_selection_data(self):
        validation = {
            "macro_f1": 0.8,
            "per_class": {label: {"f1": 0.8} for label in LABELS},
        }
        test_baseline = {
            "macro_f1": 0.1,
            "per_class": {label: {"f1": 0.1} for label in LABELS},
        }
        test_alternative = {
            "macro_f1": 0.9,
            "per_class": {label: {"f1": 0.9} for label in LABELS},
        }
        compared = _comparison(
            {
                "baseline": {
                    "status": "completed",
                    "metrics": {"validation": validation, "test": test_baseline},
                    "training_duration_seconds": 1.0,
                },
                "alternative": {
                    "status": "completed",
                    "metrics": {"validation": validation, "test": test_alternative},
                    "training_duration_seconds": 2.0,
                },
            }
        )
        self.assertEqual(compared["evaluated_split"], "validation")
        self.assertEqual(compared["higher_macro_f1"], "tie")
        self.assertTrue(compared["final_test_metrics_reported_independently"])

    def test_metrics_use_fixed_label_order_and_zero_division(self):
        metrics = _metrics_from_confusion(
            np.asarray(
                [
                    [2, 0, 0],
                    [0, 0, 1],
                    [0, 0, 1],
                ]
            )
        )
        self.assertEqual(metrics["class_order"], ["Normal", "SQLi", "XSS"])
        self.assertEqual(metrics["per_class"]["SQLi"]["precision"], 0.0)
        self.assertEqual(metrics["per_class"]["SQLi"]["recall"], 0.0)
        self.assertEqual(metrics["zero_division"], 0)
        self.assertEqual(metrics["confusion_matrix"][1], [0, 0, 1])

    def test_full_run_evaluates_both_candidates_and_writes_complete_metadata(self):
        path = self.write_dataset(per_class=30)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            result = run_training_pipeline(
                path,
                release_root=self.root / "release-root",
                config=self.config(),
                version="both-models",
            )

        metadata = result.metadata
        self.assertEqual(metadata["schema_version"], 1)
        self.assertEqual(metadata["release_version"], "both-models")
        self.assertIsInstance(metadata["created_at_utc"], str)
        self.assertEqual(
            metadata["preprocessing"],
            {
                "payload_preserved": True,
                "duplicate_fingerprint_normalization": "Unicode NFC",
                "payload_transform_for_model": "candidate-specific vectorizer",
                "threshold_selection": "not performed",
            },
        )
        self.assertEqual(
            set(metadata["library_versions"]),
            {"python", "scikit_learn", "joblib", "numpy"},
        )
        self.assertEqual(
            {name: candidate["status"] for name, candidate in metadata["candidates"].items()},
            {"alternative": "completed", "baseline": "completed"},
        )
        self.assertEqual(metadata["comparison"]["status"], "complete")
        self.assertEqual(metadata["comparison"]["evaluated_split"], "validation")
        self.assertEqual(metadata["threshold_selection"]["performed"], False)
        self.assertEqual(metadata["threshold_selection"]["test_data_used"], False)
        self.assertEqual(len(metadata["dataset_provenance"][0]["sha256"]), 64)
        self.assertEqual(metadata["training_config"]["random_seed"], 42)
        self.assertEqual(
            metadata["split_counts"]["train"],
            {"Normal": 21, "SQLi": 21, "XSS": 21},
        )
        for candidate_name in ("baseline", "alternative"):
            candidate = metadata["candidates"][candidate_name]
            self.assertEqual(candidate["name"], candidate_name)
            self.assertEqual(candidate["status"], "completed")
            self.assertEqual(candidate["model_filename"], "model.joblib")
            self.assertEqual(
                candidate["transformer_filename"],
                "feature_transform.joblib",
            )
            self.assertEqual(
                candidate["canonical_classes"],
                ["Normal", "SQLi", "XSS"],
            )
            self.assertEqual(
                candidate["algorithm"],
                {
                    "classifier": candidate["config"]["classifier"],
                    "transformer": candidate["config"]["vectorizer"],
                },
            )
            self.assertEqual(
                candidate["training_configuration"],
                candidate["config"],
            )
            self.assertEqual(
                candidate["evaluation_metrics"],
                candidate["metrics"],
            )
            self.assertEqual(
                candidate["bundle_files"],
                {
                    "model": "model.joblib",
                    "feature_transform": "feature_transform.joblib",
                },
            )
            self.assertEqual(
                candidate["metrics"]["test"]["class_order"],
                ["Normal", "SQLi", "XSS"],
            )
            self.assertEqual(
                candidate["metrics"]["test"]["total_samples"],
                sum(metadata["split_counts"]["test"].values()),
            )
        on_disk = json.loads((result.release_path / "metadata.json").read_text("utf-8"))
        self.assertEqual(on_disk["split_assignment_sha256"], metadata["split_assignment_sha256"])
        self.assertTrue((result.release_path / "label_mapping.json").is_file())
        self.assertTrue((result.release_path / "preprocessing.json").is_file())

        import joblib

        for candidate_name in ("baseline", "alternative"):
            model = joblib.load(result.release_path / candidate_name / "model.joblib")
            transform = joblib.load(
                result.release_path / candidate_name / "feature_transform.joblib"
            )
            features = transform.transform(["test payload"])
            self.assertTrue(hasattr(model, "predict"))
            self.assertTrue(hasattr(model, "predict_proba"))
            self.assertEqual(set(model.classes_), set(LABELS))
            self.assertEqual(
                list(model.classes_),
                metadata["candidates"][candidate_name]["classes"],
            )
            self.assertEqual(model.predict_proba(features).shape, (1, 3))

    def test_insufficient_class_failure_publishes_no_release_and_cleans_temp(self):
        path = self.write_dataset(per_class=2)
        # A conflicting Normal/SQLi payload removes one unique sample from each class.
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(
                json.dumps({"payload": "shared conflict", "label": "Normal"}) + "\n"
            )
            stream.write(
                json.dumps({"payload": "shared conflict", "label": "SQLi"}) + "\n"
            )
        temporary = self.root / "failure-temp"
        temporary.mkdir()
        release_root = self.root / "failed-releases"

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            with self.assertRaises(TrainingPipelineError):
                run_training_pipeline(
                    path,
                    release_root=release_root,
                    temp_root=temporary,
                    config=self.config(),
                    version="must-not-publish",
                )
        self.assertEqual(list(temporary.iterdir()), [])
        self.assertFalse((release_root / "must-not-publish").exists())

    def test_existing_release_is_never_overwritten(self):
        path = self.write_dataset()
        release_root = self.root / "releases"
        existing = release_root / "immutable-release"
        existing.mkdir(parents=True)
        sentinel = existing / "keep.txt"
        sentinel.write_text("unchanged", encoding="utf-8")

        with self.assertRaises(FileExistsError):
            run_training_pipeline(
                path,
                release_root=release_root,
                version="immutable-release",
                config=self.config(),
            )
        self.assertEqual(sentinel.read_text("utf-8"), "unchanged")

    def test_failed_release_publication_cleans_staging_and_keeps_old_release(self):
        release_root = self.root / "releases"
        release_root.mkdir()
        existing = release_root / "previous"
        existing.mkdir()
        (existing / "sentinel").write_text("safe", encoding="utf-8")
        candidates = {
            "alternative": {
                "status": "completed",
                "model": object(),
                "vectorizer": object(),
                "classes": list(LABELS),
            }
        }

        with mock.patch("training_pipeline.joblib.dump", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                _publish_release(release_root, "new-release", candidates, {})
        self.assertFalse((release_root / "new-release").exists())
        self.assertEqual((existing / "sentinel").read_text("utf-8"), "safe")
        self.assertEqual(
            [entry.name for entry in release_root.iterdir()],
            ["previous"],
        )

    def test_release_destination_collision_does_not_replace_racing_release(self):
        release_root = self.root / "collision-releases"
        release_root.mkdir()

        def create_racing_release(source, destination):
            self.assertTrue(source.is_dir())
            destination.mkdir()
            (destination / "sentinel.txt").write_text("preserve", encoding="utf-8")
            raise FileExistsError(str(destination))

        with mock.patch(
            "training_pipeline._rename_directory_noreplace",
            side_effect=create_racing_release,
        ):
            with self.assertRaises(FileExistsError):
                _publish_release(
                    release_root,
                    "racing-release",
                    {},
                    {},
                )

        self.assertEqual(
            (release_root / "racing-release" / "sentinel.txt").read_text("utf-8"),
            "preserve",
        )
        entries = [entry.name for entry in release_root.iterdir()]
        self.assertIn("racing-release", entries)
        staging_names = [
            name for name in entries if name.startswith(".racing-release.staging-")
        ]
        self.assertEqual(len(staging_names), 1)

    def test_release_rename_success_publishes_staging_directory(self):
        release_root = self.root / "successful-releases"
        result = _publish_release(
            release_root,
            "published-release",
            {},
            {"release_version": "published-release"},
        )

        self.assertEqual(result, release_root / "published-release")
        self.assertTrue((result / "metadata.json").is_file())
        self.assertFalse(any(release_root.glob(".published-release.staging-*")))

    def test_permission_error_during_rename_is_logged_and_preserves_staging(self):
        release_root = self.root / "rename-failure-releases"
        rename_error = PermissionError(5, "Access denied")
        rename_error.winerror = 5

        with mock.patch(
            "training_pipeline._rename_directory_noreplace",
            side_effect=rename_error,
        ) as rename, self.assertLogs("training_pipeline", level="ERROR") as logs:
            with self.assertRaises(PermissionError) as raised:
                _publish_release(
                    release_root,
                    "permission-failure",
                    {},
                    {"release_version": "permission-failure"},
                )

        self.assertIs(raised.exception, rename_error)
        source, destination = rename.call_args.args
        self.assertTrue(source.is_dir())
        self.assertFalse(destination.exists())
        self.assertIn(str(source.resolve()), logs.output[0])
        self.assertIn(str(destination.resolve()), logs.output[0])
        self.assertIn("exception_class=PermissionError", logs.output[0])
        self.assertIn("errno=5", logs.output[0])
        self.assertIn("winerror=5", logs.output[0])
        self.assertIn("Access denied", logs.output[0])
        self.assertIn("incomplete/unpublished", logs.output[0])

    def test_diagnostic_stat_error_does_not_mask_rename_error(self):
        release_root = self.root / "diagnostic-stat-failure-releases"
        rename_error = PermissionError(5, "Access denied")
        source_holder = {}
        rename_attempted = False
        original_stat = Path.stat

        def fail_source_stat_after_rename_attempt(path, *args, **kwargs):
            if rename_attempted and path == source_holder.get("source"):
                raise OSError("diagnostic stat failed")
            return original_stat(path, *args, **kwargs)

        def fail_rename(source, destination):
            nonlocal rename_attempted
            source_holder["source"] = source
            rename_attempted = True
            raise rename_error

        with mock.patch.object(
            Path, "stat", fail_source_stat_after_rename_attempt
        ), mock.patch(
            "training_pipeline._rename_directory_noreplace",
            side_effect=fail_rename,
        ), self.assertLogs("training_pipeline", level="ERROR") as logs:
            with self.assertRaises(PermissionError) as raised:
                _publish_release(
                    release_root,
                    "diagnostic-stat-failure",
                    {},
                    {"release_version": "diagnostic-stat-failure"},
                )

        self.assertIs(raised.exception, rename_error)
        self.assertIn("source_stat: OSError: diagnostic stat failed", logs.output[0])
        self.assertIn("incomplete/unpublished", logs.output[0])
        self.assertTrue(source_holder["source"].is_dir())

    def test_staging_name_is_rejected_as_model_release(self):
        from api.model_loader import ModelLoaderError, load_model_release

        release_root = self.root / "loader-releases"
        rename_error = PermissionError(5, "Access denied")
        source_holder = {}

        def fail_rename(source, _destination):
            source_holder["source"] = source
            raise rename_error

        with mock.patch(
            "training_pipeline._rename_directory_noreplace",
            side_effect=fail_rename,
        ), mock.patch("training_pipeline.logger.error"):
            with self.assertRaises(PermissionError):
                _publish_release(
                    release_root,
                    "loader-staging",
                    {},
                    {"release_version": "loader-staging"},
                )

        staging = source_holder["source"]
        self.assertTrue(staging.is_dir())
        with self.assertRaisesRegex(ModelLoaderError, "safe release name"):
            load_model_release(release_root, staging.name, "alternative")

    def test_platform_specific_no_clobber_dispatch(self):
        source = self.root / "source"
        destination = self.root / "destination"
        source.mkdir()

        with mock.patch("training_pipeline.platform.system", return_value="Windows"):
            with mock.patch("training_pipeline.os.rename") as windows_rename:
                _rename_directory_noreplace(source, destination)
                windows_rename.assert_called_once_with(source, destination)

        with mock.patch("training_pipeline.platform.system", return_value="Linux"):
            with mock.patch(
                "training_pipeline._rename_directory_noreplace_posix"
            ) as linux_rename:
                _rename_directory_noreplace(source, destination)
                linux_rename.assert_called_once_with(source, destination)

        with mock.patch("training_pipeline.platform.system", return_value="Darwin"):
            with self.assertRaisesRegex(TrainingPipelineError, "supported only"):
                _rename_directory_noreplace(source, destination)

    @unittest.skipUnless(
        __import__("platform").system() in {"Windows", "Linux"},
        "No-clobber rename integration is supported only on Windows/Linux",
    )
    def test_actual_platform_rename_does_not_replace_existing_release(self):
        source = self.root / "staging"
        destination = self.root / "existing"
        source.mkdir()
        destination.mkdir()
        (source / "staging.txt").write_text("staging", encoding="utf-8")
        sentinel = destination / "sentinel.txt"
        sentinel.write_text("existing release", encoding="utf-8")

        with self.assertRaises(FileExistsError):
            _rename_directory_noreplace(source, destination)

        self.assertTrue(source.is_dir())
        self.assertEqual(sentinel.read_text("utf-8"), "existing release")
        self.assertFalse((destination / "staging.txt").exists())

    @unittest.skipUnless(
        __import__("platform").system() in {"Windows", "Linux"},
        "Process-level no-clobber publication is supported only on Windows/Linux",
    )
    def test_two_processes_publish_same_name_without_overwriting_winner(self):
        context = multiprocessing.get_context("spawn")
        release_root = self.root / "concurrent-releases"
        release_root.mkdir()
        ready = context.Queue()
        results = context.Queue()
        start = context.Event()
        processes = [
            context.Process(
                target=_publish_from_process,
                args=(
                    str(release_root),
                    "contended",
                    f"worker-{index}",
                    ready,
                    start,
                    results,
                ),
            )
            for index in range(2)
        ]
        for process in processes:
            process.start()
        try:
            self.assertTrue(ready.get(timeout=20))
            self.assertTrue(ready.get(timeout=20))
            start.set()
            outcomes = [results.get(timeout=30) for _ in processes]
        finally:
            start.set()
            for process in processes:
                process.join(timeout=30)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
        self.assertCountEqual(
            [outcome[0] for outcome in outcomes],
            ["published", "collision"],
        )
        self.assertTrue(all(process.exitcode == 0 for process in processes))
        release_metadata = json.loads(
            (release_root / "contended" / "metadata.json").read_text("utf-8")
        )
        winner = next(value for status, value in outcomes if status == "published")
        self.assertEqual(release_metadata, {"process": winner})
        entries = list(release_root.iterdir())
        self.assertIn(release_root / "contended", entries)
        unpublished_staging = [
            entry
            for entry in entries
            if entry.name.startswith(".contended.staging-")
        ]
        self.assertLessEqual(len(unpublished_staging), 1)
        self.assertTrue(all(entry.is_dir() for entry in unpublished_staging))

    def test_failed_bundle_contract_validation_does_not_publish_release(self):
        release_root = self.root / "invalid-bundle-releases"
        candidate = {
            "status": "completed",
            "model": object(),
            "vectorizer": object(),
            "classes": list(LABELS),
        }
        with self.assertRaisesRegex(
            TrainingPipelineError, "does not expose predict"
        ):
            _publish_release(
                release_root,
                "invalid-bundle",
                {"alternative": candidate},
                {},
            )
        self.assertFalse((release_root / "invalid-bundle").exists())
        self.assertEqual(list(release_root.iterdir()), [])

    def test_failed_serialization_does_not_publish_release(self):
        release_root = self.root / "serialization-failure-releases"
        candidate = {
            "status": "completed",
            "model": object(),
            "vectorizer": object(),
            "classes": list(LABELS),
        }
        with mock.patch(
            "training_pipeline.joblib.dump",
            side_effect=OSError("serialization storage failure"),
        ):
            with self.assertRaisesRegex(OSError, "serialization storage failure"):
                _publish_release(
                    release_root,
                    "serialization-failure",
                    {"alternative": candidate},
                    {},
                )
        self.assertFalse((release_root / "serialization-failure").exists())
        self.assertEqual(list(release_root.iterdir()), [])

    def test_deterministic_seed_is_recorded_and_release_is_unique(self):
        path = self.write_dataset(per_class=6)
        releases = self.root / "seed-releases"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            first = run_training_pipeline(
                path,
                release_root=releases,
                config=self.config(random_seed=7),
            )
        self.assertEqual(first.metadata["random_seed"], 7)
        self.assertEqual(first.metadata["training_config"]["random_seed"], 7)
        self.assertTrue(first.release_path.is_dir())


if __name__ == "__main__":
    unittest.main()
