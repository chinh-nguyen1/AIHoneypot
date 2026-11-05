import json
import platform
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import RandomForestClassifier
from sklearn.feature_extraction.text import HashingVectorizer, TfidfVectorizer
from sklearn.linear_model import SGDClassifier

AI_ENGINE_ROOT = Path(__file__).resolve().parents[1]
if str(AI_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_ENGINE_ROOT))

import api.model_loader as model_loader
from api.model_loader import (
    CANONICAL_CLASSES,
    LoadedModel,
    ModelLoaderError,
    load_model_release,
)


class _ContractTransformer:
    def transform(self, payloads):
        return np.ones((len(payloads), 2), dtype=float)


class _ContractModel:
    def __init__(self, probabilities=(0.8, 0.1, 0.1), prediction="Normal"):
        self.classes_ = list(CANONICAL_CLASSES)
        self._probabilities = probabilities
        self._prediction = prediction

    def predict(self, features):
        return np.asarray([self._prediction] * features.shape[0])

    def predict_proba(self, features):
        return np.asarray(
            [self._probabilities] * features.shape[0],
            dtype=float,
        )


class _NoPredictModel:
    classes_ = list(CANONICAL_CLASSES)

    def predict_proba(self, features):
        return np.asarray([[0.8, 0.1, 0.1]])


class _NoPredictProbaModel:
    classes_ = list(CANONICAL_CLASSES)

    def predict(self, features):
        return np.asarray(["Normal"])


class _NoTransform:
    pass


class _BadShapeModel(_ContractModel):
    def predict_proba(self, features):
        return np.asarray([[0.8, 0.2]])


class ModelLoaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_directory = tempfile.TemporaryDirectory()
        cls.fixture_root = Path(cls.fixture_directory.name) / "releases"
        cls.fixture_root.mkdir()
        release_dir = cls.fixture_root / "release-1"
        release_dir.mkdir()
        examples = [
            "ordinary home page request",
            "ordinary product listing",
            "select user from account where id=1",
            "' OR 1=1 --",
            "<script>alert(1)</script>",
            "img src=x onerror=alert(1)",
        ]
        labels = ["Normal", "Normal", "SQLi", "SQLi", "XSS", "XSS"]

        alternative_transformer = HashingVectorizer(
            analyzer="char",
            ngram_range=(1, 3),
            alternate_sign=False,
            n_features=64,
        )
        alternative_features = alternative_transformer.transform(examples)
        alternative_model = SGDClassifier(loss="log_loss", random_state=17)
        alternative_model.partial_fit(
            alternative_features,
            labels,
            classes=np.asarray(CANONICAL_CLASSES, dtype=object),
        )

        baseline_transformer = TfidfVectorizer(
            analyzer="char",
            ngram_range=(1, 3),
        )
        baseline_features = baseline_transformer.fit_transform(examples)
        baseline_model = RandomForestClassifier(
            n_estimators=5,
            random_state=17,
        ).fit(baseline_features, labels)

        for candidate_name, model, transformer in (
            ("alternative", alternative_model, alternative_transformer),
            ("baseline", baseline_model, baseline_transformer),
        ):
            candidate_dir = release_dir / candidate_name
            candidate_dir.mkdir()
            joblib.dump(model, candidate_dir / "model.joblib")
            joblib.dump(transformer, candidate_dir / "feature_transform.joblib")

        cls.base_metadata = {
            "schema_version": 1,
            "release_version": "release-1",
            "created_at_utc": "2026-11-01T00:00:00+00:00",
            "library_versions": {
                "python": platform.python_version(),
                "scikit_learn": sklearn.__version__,
                "joblib": joblib.__version__,
                "numpy": np.__version__,
            },
            "preprocessing": {
                "payload_preserved": True,
            },
            "candidates": {},
        }
        for candidate_name, classifier, transformer in (
            ("alternative", "SGDClassifier", "HashingVectorizer"),
            ("baseline", "RandomForestClassifier", "TfidfVectorizer"),
        ):
            cls.base_metadata["candidates"][candidate_name] = {
                "name": candidate_name,
                "status": "completed",
                "model_filename": "model.joblib",
                "transformer_filename": "feature_transform.joblib",
                "algorithm": {
                    "classifier": classifier,
                    "transformer": transformer,
                },
                "canonical_classes": list(CANONICAL_CLASSES),
                "training_configuration": {"random_state": 17},
                "evaluation_metrics": {
                    "validation": {"macro_f1": 0.9},
                    "test": {"macro_f1": 0.8},
                },
            }
        cls._write_metadata(release_dir / "metadata.json", cls.base_metadata)

    @classmethod
    def tearDownClass(cls):
        cls.fixture_directory.cleanup()

    @staticmethod
    def _write_metadata(path, metadata):
        path.write_text(
            json.dumps(metadata),
            encoding="utf-8",
        )

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "releases"
        shutil.copytree(self.fixture_root, self.root)
        self.release_dir = self.root / "release-1"
        self.metadata_path = self.release_dir / "metadata.json"

    def read_metadata(self):
        return json.loads(self.metadata_path.read_text(encoding="utf-8"))

    def write_metadata(self, metadata):
        self._write_metadata(self.metadata_path, metadata)

    def load(self, candidate="alternative", version="release-1"):
        return load_model_release(self.root, version, candidate)

    def replace_candidate_model(self, candidate, model):
        joblib.dump(
            model,
            self.release_dir / candidate / "model.joblib",
        )

    def test_loads_valid_alternative_sgd_bundle(self):
        loaded = self.load("alternative")

        self.assertIsInstance(loaded, LoadedModel)
        self.assertEqual(loaded.release_version, "release-1")
        self.assertEqual(loaded.candidate_name, "alternative")
        self.assertIsInstance(loaded.model, SGDClassifier)
        self.assertIsInstance(loaded.transformer, HashingVectorizer)

    def test_loads_valid_baseline_random_forest_bundle(self):
        loaded = self.load("baseline")

        self.assertIsInstance(loaded.model, RandomForestClassifier)
        self.assertIsInstance(loaded.transformer, TfidfVectorizer)

    def test_missing_release_is_rejected(self):
        with self.assertRaisesRegex(ModelLoaderError, "release directory"):
            self.load(version="missing-release")

    def test_missing_candidate_is_rejected(self):
        metadata = self.read_metadata()
        del metadata["candidates"]["alternative"]
        self.write_metadata(metadata)

        with self.assertRaisesRegex(ModelLoaderError, "is missing"):
            self.load()

    def test_candidate_outside_allowlist_is_rejected(self):
        with self.assertRaisesRegex(ModelLoaderError, "AI_MODEL_CANDIDATE"):
            self.load(candidate="../baseline")

    def test_incomplete_candidate_is_rejected(self):
        metadata = self.read_metadata()
        metadata["candidates"]["alternative"]["status"] = "failed"
        self.write_metadata(metadata)

        with self.assertRaisesRegex(ModelLoaderError, "not completed"):
            self.load()

    def test_metadata_missing_required_field_is_rejected(self):
        metadata = self.read_metadata()
        del metadata["preprocessing"]
        self.write_metadata(metadata)

        with self.assertRaisesRegex(ModelLoaderError, "preprocessing"):
            self.load()

    def test_metadata_with_wrong_field_type_is_rejected(self):
        metadata = self.read_metadata()
        metadata["candidates"]["alternative"]["training_configuration"] = []
        self.write_metadata(metadata)

        with self.assertRaisesRegex(ModelLoaderError, "training_configuration"):
            self.load()

    def test_unsupported_schema_version_is_rejected_before_deserialization(self):
        metadata = self.read_metadata()
        metadata["schema_version"] = 2
        self.write_metadata(metadata)

        with mock.patch.object(model_loader.joblib, "load") as load_artifact:
            with self.assertRaisesRegex(ModelLoaderError, "Unsupported"):
                self.load()
        load_artifact.assert_not_called()

    def test_metadata_release_version_must_match_requested_release(self):
        metadata = self.read_metadata()
        metadata["release_version"] = "release-2"
        self.write_metadata(metadata)

        with self.assertRaisesRegex(ModelLoaderError, "does not match"):
            self.load()

    def test_invalid_canonical_class_lists_are_rejected(self):
        invalid_values = (
            ["Normal", "SQLi"],
            ["Normal", "SQLi", "RCE"],
            ["Normal", "SQLi", "SQLi"],
        )
        for classes in invalid_values:
            with self.subTest(classes=classes):
                metadata = self.read_metadata()
                metadata["candidates"]["alternative"]["canonical_classes"] = classes
                self.write_metadata(metadata)

                with self.assertRaisesRegex(ModelLoaderError, "canonical_classes"):
                    self.load()

    def test_loaded_model_classes_must_match_metadata(self):
        model = _ContractModel()
        model.classes_ = ["Normal", "SQLi", "RCE"]
        self.replace_candidate_model("alternative", model)

        with self.assertRaisesRegex(ModelLoaderError, "classes_"):
            self.load()

    def test_missing_model_artifact_is_rejected(self):
        (self.release_dir / "alternative" / "model.joblib").unlink()

        with self.assertRaisesRegex(ModelLoaderError, "missing or resolves"):
            self.load()

    def test_corrupt_model_artifact_is_rejected(self):
        (self.release_dir / "alternative" / "model.joblib").write_bytes(
            b"not a serialized model"
        )

        with self.assertRaisesRegex(ModelLoaderError, "deserialize"):
            self.load()

    def test_release_path_traversal_is_rejected(self):
        with self.assertRaisesRegex(ModelLoaderError, "safe release name"):
            self.load(version="../release-1")

    def test_absolute_release_path_is_rejected(self):
        with self.assertRaisesRegex(ModelLoaderError, "safe release name"):
            self.load(version=str(self.release_dir.resolve()))

    def test_artifact_path_traversal_is_rejected(self):
        metadata = self.read_metadata()
        metadata["candidates"]["alternative"]["model_filename"] = "../model.joblib"
        self.write_metadata(metadata)

        with self.assertRaisesRegex(ModelLoaderError, "filename is not allowed"):
            self.load()

    def test_artifact_symlink_outside_candidate_is_rejected(self):
        candidate_dir = self.release_dir / "alternative"
        model_path = candidate_dir / "model.joblib"
        external_model = self.root / "external-model.joblib"
        shutil.copyfile(model_path, external_model)
        model_path.unlink()
        try:
            model_path.symlink_to(external_model)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Symlink creation is unavailable: {exc}")

        with self.assertRaisesRegex(ModelLoaderError, "outside"):
            self.load()

    def test_dependency_mismatch_is_rejected_before_deserialization(self):
        metadata = self.read_metadata()
        metadata["library_versions"]["joblib"] = "0.0"
        self.write_metadata(metadata)

        with mock.patch.object(model_loader.joblib, "load") as load_artifact:
            with self.assertRaisesRegex(ModelLoaderError, "joblib version mismatch"):
                self.load()
        load_artifact.assert_not_called()

    def test_transformer_without_transform_is_rejected(self):
        joblib.dump(
            _NoTransform(),
            self.release_dir / "alternative" / "feature_transform.joblib",
        )

        with self.assertRaisesRegex(ModelLoaderError, "transform"):
            self.load()

    def test_model_without_predict_is_rejected(self):
        self.replace_candidate_model("alternative", _NoPredictModel())

        with self.assertRaisesRegex(ModelLoaderError, "predict"):
            self.load()

    def test_model_without_predict_proba_is_rejected(self):
        self.replace_candidate_model("alternative", _NoPredictProbaModel())

        with self.assertRaisesRegex(ModelLoaderError, "predict_proba"):
            self.load()

    def test_probability_shape_must_be_one_by_three(self):
        self.replace_candidate_model("alternative", _BadShapeModel())
        with self.assertRaisesRegex(ModelLoaderError, "shape"):
            self.load()

    def test_invalid_probability_values_are_rejected(self):
        for values in (
            (float("nan"), 0.1, 0.9),
            (float("inf"), 0.0, 0.0),
            (-0.1, 0.5, 0.6),
            (1.1, 0.0, -0.1),
            (0.8, 0.1, 0.2),
        ):
            with self.subTest(values=values):
                self.replace_candidate_model(
                    "alternative",
                    _ContractModel(probabilities=values),
                )
                with self.assertRaisesRegex(ModelLoaderError, "invalid probabilities"):
                    self.load()

    def test_prediction_outside_taxonomy_is_rejected(self):
        self.replace_candidate_model(
            "alternative",
            _ContractModel(prediction="Unknown"),
        )

        with self.assertRaisesRegex(ModelLoaderError, "outside the taxonomy"):
            self.load()


if __name__ == "__main__":
    unittest.main()
