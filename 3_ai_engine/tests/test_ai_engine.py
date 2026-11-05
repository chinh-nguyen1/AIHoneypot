import builtins
import importlib.util
import math
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


AI_ENGINE_ROOT = Path(__file__).resolve().parents[1]


class _HTTPException(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _FastAPI:
    def __init__(self, **kwargs):
        pass

    def add_middleware(self, *args, **kwargs):
        return None

    def get(self, *args, **kwargs):
        return lambda function: function

    def post(self, *args, **kwargs):
        return lambda function: function


class _PayloadRequest:
    def __init__(self, payload):
        self.payload = payload


def _module(name, **attributes):
    result = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(result, key, value)
    return result


def _load_main(
    model_release="",
    model_candidate="",
    loaded_model=None,
    loader_error=None,
):
    fastapi = _module(
        "fastapi",
        FastAPI=_FastAPI,
        HTTPException=_HTTPException,
    )
    cors_module = _module("fastapi.middleware.cors", CORSMiddleware=object())
    middleware_module = _module("fastapi.middleware")
    middleware_module.__path__ = []
    pydantic = _module("pydantic", BaseModel=object)
    connector = _module("mysql.connector", connect=Mock())
    mysql = _module("mysql")
    mysql.__path__ = []
    mysql.connector = connector
    api = _module("api")
    api.__path__ = [str(AI_ENGINE_ROOT / "api")]
    schemas = _module(
        "api.schemas",
        AnalysisResponse=object,
        CANONICAL_ATTACK_TYPES=("Normal", "SQLi", "XSS"),
        PayloadRequest=_PayloadRequest,
    )
    database_config = _module("api.database_config", get_mysql_config=Mock())
    loader_error_type = type("ModelLoaderError", (RuntimeError,), {})
    model_loader = _module(
        "api.model_loader",
        ModelLoaderError=loader_error_type,
        load_model_release=Mock(),
    )
    if loader_error is not None:
        model_loader.load_model_release.side_effect = (
            loader_error
            if isinstance(loader_error, BaseException)
            else loader_error_type(str(loader_error))
        )
    elif loaded_model is not None:
        model_loader.load_model_release.return_value = loaded_model
    else:
        model_loader.load_model_release.side_effect = loader_error_type(
            "release configuration is unavailable"
        )

    modules = {
        "fastapi": fastapi,
        "fastapi.middleware": middleware_module,
        "fastapi.middleware.cors": cors_module,
        "pydantic": pydantic,
        "mysql": mysql,
        "mysql.connector": connector,
        "api": api,
        "api.schemas": schemas,
        "api.database_config": database_config,
        "api.model_loader": model_loader,
    }
    with patch.dict(
        os.environ,
        {
            "AI_MODEL_RELEASE": model_release,
            "AI_MODEL_CANDIDATE": model_candidate,
        },
    ):
        forbidden_imports = []
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "api.llm_advisor":
                forbidden_imports.append(name)
                raise AssertionError("Local-only AI Engine imported Gemini adapter")
            return original_import(name, *args, **kwargs)

        with patch.dict(sys.modules, modules), patch(
            "builtins.__import__", side_effect=guarded_import
        ):
            spec = importlib.util.spec_from_file_location(
                "ai_engine_test_main", AI_ENGINE_ROOT / "api" / "main.py"
            )
            loaded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loaded)
    loaded.PayloadRequest = _PayloadRequest
    return loaded, model_loader, forbidden_imports


class _Vectorizer:
    def transform(self, payloads):
        return payloads


class _Model:
    def __init__(self, label, confidence=0.95):
        self.classes_ = ["Normal", "SQLi", "XSS"]
        self.label = label
        self.confidence = confidence

    def predict(self, features):
        return [self.label]

    def predict_proba(self, features):
        other_probability = (1 - self.confidence) / 2
        return [
            [
                self.confidence if label == self.label else other_probability
                for label in self.classes_
            ]
        ]


class AIEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module, cls.model_loader, cls.forbidden_imports = _load_main()

    def setUp(self):
        self.module.model = None
        self.module.vectorizer = None
        self.module.LOCAL_MODEL_LOAD_ERROR = "FileNotFoundError"

    def _set_model(self, label, confidence=0.95):
        self.module.model = _Model(label, confidence)
        self.module.vectorizer = _Vectorizer()

    def test_high_confidence_local_predictions_preserve_canonical_labels(self):
        for label, risk_score in (("Normal", 1), ("SQLi", 9), ("XSS", 8)):
            with self.subTest(label=label):
                self._set_model(label)
                result = self.module.analyze(_PayloadRequest("payload"))
                self.assertEqual(result["source"], "Local_ML")
                self.assertEqual(result["classification_status"], "CLASSIFIED")
                self.assertEqual(result["attack_type"], label)
                self.assertEqual(result["confidence"], 0.95)
                self.assertEqual(result["risk_score"], risk_score)
                self.assertIsNone(result["reason"])
                self.assertIn("recommendation", result)
                if label == "SQLi":
                    self.assertEqual(
                        result["recommendation"],
                        "Review SQL query construction and use parameterized statements.",
                    )
                elif label == "XSS":
                    self.assertEqual(
                        result["recommendation"],
                        "Review input handling and apply context-appropriate output encoding.",
                    )
                else:
                    self.assertIsNone(result["recommendation"])

    def test_low_confidence_returns_needs_review_without_a_label(self):
        self._set_model("XSS", confidence=0.7)

        result = self.module.analyze(_PayloadRequest("ambiguous request"))

        self.assertEqual(result["source"], "Local_ML")
        self.assertEqual(result["classification_status"], "NEEDS_REVIEW")
        self.assertEqual(result["reason"], "LOW_CONFIDENCE")
        self.assertIsNone(result["attack_type"])
        self.assertEqual(result["confidence"], 0.7)
        self.assertIsNone(result["risk_score"])
        self.assertIsNone(result["recommendation"])

    def test_confidence_at_threshold_is_classified_and_below_is_review(self):
        self._set_model("Normal", confidence=0.85)
        classified = self.module.analyze(_PayloadRequest("payload"))
        self.assertEqual(classified["classification_status"], "CLASSIFIED")
        self.assertEqual(classified["confidence"], 0.85)

        self._set_model("Normal", confidence=0.849999)
        review = self.module.analyze(_PayloadRequest("payload"))
        self.assertEqual(review["classification_status"], "NEEDS_REVIEW")
        self.assertEqual(review["reason"], "LOW_CONFIDENCE")
        self.assertIsNone(review["attack_type"])

    def test_model_unavailable_returns_needs_review_without_a_label(self):
        result = self.module.analyze(_PayloadRequest("payload"))

        self.assertEqual(result["classification_status"], "NEEDS_REVIEW")
        self.assertEqual(result["reason"], "MODEL_UNAVAILABLE")
        self.assertIsNone(result["attack_type"])
        self.assertIsNone(result["confidence"])
        self.assertIsNone(result["risk_score"])
        self.assertIsNone(result["recommendation"])

    def test_model_load_failure_is_reported_as_model_unavailable(self):
        self.module.AI_MODEL_RELEASE = "release-1"
        self.module.AI_MODEL_CANDIDATE = "alternative"
        self.model_loader.load_model_release.reset_mock(
            return_value=True, side_effect=True
        )
        self.model_loader.load_model_release.side_effect = (
            self.model_loader.ModelLoaderError("unreadable artifact")
        )
        model, vectorizer, error = self.module._load_local_model()
        self.assertIsNone(model)
        self.assertIsNone(vectorizer)
        self.assertEqual(error, "ModelLoaderError")
        self.model_loader.load_model_release.assert_called_once_with(
            self.module.MODEL_RELEASE_ROOT,
            "release-1",
            "alternative",
        )

        self.module.model = model
        self.module.vectorizer = vectorizer
        result = self.module.analyze(_PayloadRequest("payload"))
        self.assertEqual(result["classification_status"], "NEEDS_REVIEW")
        self.assertEqual(result["reason"], "MODEL_UNAVAILABLE")
        self.assertNotIn("unreadable artifact", str(result))
        self.assertNotIn("Traceback", str(result))

    def test_api_loads_only_explicitly_configured_candidate(self):
        configured = type(
            "ConfiguredModel",
            (),
            {"model": _Model("XSS"), "transformer": _Vectorizer()},
        )()
        module, loader, forbidden_imports = _load_main(
            "release-42",
            "baseline",
            loaded_model=configured,
        )

        self.assertIs(module.model, configured.model)
        self.assertIs(module.vectorizer, configured.transformer)
        loader.load_model_release.assert_called_once_with(
            module.MODEL_RELEASE_ROOT,
            "release-42",
            "baseline",
        )
        self.assertEqual(module.analyze(_PayloadRequest("payload"))["source"], "Local_ML")
        self.assertEqual(forbidden_imports, [])

    def test_missing_model_configuration_returns_review_result(self):
        module, loader, _ = _load_main()

        result = module.analyze(_PayloadRequest("payload"))

        self.assertIsNone(module.model)
        self.assertEqual(module.LOCAL_MODEL_LOAD_ERROR, "ModelLoaderError")
        self.assertEqual(result["classification_status"], "NEEDS_REVIEW")
        self.assertEqual(result["reason"], "MODEL_UNAVAILABLE")
        loader.load_model_release.assert_called_once_with(
            module.MODEL_RELEASE_ROOT,
            "",
            "",
        )

    def test_configured_loader_failure_does_not_select_another_candidate(self):
        module, loader, _ = _load_main(
            "missing-release",
            "alternative",
            loader_error="configured release missing",
        )

        self.assertIsNone(module.model)
        loader.load_model_release.assert_called_once_with(
            module.MODEL_RELEASE_ROOT,
            "missing-release",
            "alternative",
        )

    def test_loaded_release_preserves_existing_fields_and_adds_status(self):
        configured = type(
            "ConfiguredModel",
            (),
            {"model": _Model("SQLi"), "transformer": _Vectorizer()},
        )()
        module, _, _ = _load_main(
            "release-42",
            "alternative",
            loaded_model=configured,
        )

        response = module.analyze(_PayloadRequest("payload"))

        self.assertEqual(
            set(response),
            {
                "source",
                "classification_status",
                "attack_type",
                "confidence",
                "risk_score",
                "reason",
                "recommendation",
            },
        )
        self.assertEqual(response["source"], "Local_ML")
        self.assertEqual(response["classification_status"], "CLASSIFIED")
        self.assertEqual(response["attack_type"], "SQLi")

    def test_inference_exception_returns_review_without_exposing_exception(self):
        class BrokenModel(_Model):
            def predict(self, features):
                raise RuntimeError("private internal detail")

        self.module.model = BrokenModel("XSS")
        self.module.vectorizer = _Vectorizer()

        result = self.module.analyze(_PayloadRequest("payload"))

        self.assertEqual(result["classification_status"], "NEEDS_REVIEW")
        self.assertEqual(result["reason"], "INFERENCE_ERROR")
        self.assertIsNone(result["attack_type"])
        self.assertIsNone(result["confidence"])
        self.assertNotIn("private internal detail", str(result))
        self.assertNotIn("Traceback", str(result))

    def test_invalid_model_output_is_not_coerced_to_normal(self):
        self._set_model("Unknown")

        result = self.module.analyze(_PayloadRequest("payload"))

        self.assertEqual(result["classification_status"], "NEEDS_REVIEW")
        self.assertEqual(result["reason"], "INFERENCE_ERROR")
        self.assertIsNone(result["attack_type"])
        self.assertIsNone(result["risk_score"])
        self.assertIsNone(result["confidence"])
        self.assertNotIn("Normal", str(result))

    def test_invalid_model_probabilities_return_inference_error(self):
        class InvalidProbabilityModel(_Model):
            def predict_proba(self, features):
                return [[float("nan"), 0.1, 0.9]]

        self.module.model = InvalidProbabilityModel("XSS")
        self.module.vectorizer = _Vectorizer()
        result = self.module.analyze(_PayloadRequest("payload"))

        self.assertEqual(result["classification_status"], "NEEDS_REVIEW")
        self.assertEqual(result["reason"], "INFERENCE_ERROR")
        self.assertIsNone(result["confidence"])

    def test_local_only_never_imports_gemini_adapter(self):
        for model_state in ("classified", "review", "unavailable", "inference_error"):
            with self.subTest(model_state=model_state):
                if model_state == "classified":
                    self._set_model("SQLi")
                elif model_state == "review":
                    self._set_model("SQLi", confidence=0.7)
                elif model_state == "inference_error":
                    class BrokenModel(_Model):
                        def predict(self, features):
                            raise RuntimeError("private internal detail")

                    self.module.model = BrokenModel("SQLi")
                    self.module.vectorizer = _Vectorizer()
                else:
                    self.module.model = None
                    self.module.vectorizer = None

                self.module.analyze(_PayloadRequest("payload"))

        self.assertEqual(self.forbidden_imports, [])

    def test_only_canonical_labels_are_accepted_by_api(self):
        self.assertEqual(
            self.module.VALID_ATTACK_TYPES,
            frozenset({"Normal", "SQLi", "XSS"}),
        )
        self.assertEqual(self.forbidden_imports, [])

    def test_valid_response_matches_pipeline_classification_contract(self):
        self._set_model("SQLi")
        result = self.module.analyze(_PayloadRequest("payload"))

        self.assertEqual(
            set(result),
            {
                "source",
                "classification_status",
                "attack_type",
                "confidence",
                "risk_score",
                "reason",
                "recommendation",
            },
        )
        self.assertEqual(result["classification_status"], "CLASSIFIED")
        self.assertIsInstance(result["attack_type"], str)
        self.assertIsInstance(result["confidence"], float)
        self.assertTrue(math.isfinite(result["confidence"]))
        self.assertGreaterEqual(result["confidence"], 0)
        self.assertLessEqual(result["confidence"], 1)
        self.assertIsInstance(result["risk_score"], int)
        self.assertGreaterEqual(result["risk_score"], 1)
        self.assertLessEqual(result["risk_score"], 10)


class _Frame:
    columns = ("payload", "label")

    def __init__(self, payloads, labels):
        self.values = {"payload": payloads, "label": labels}
        self.empty = not payloads

    def __len__(self):
        return len(self.values["payload"])

    def __getitem__(self, column):
        return self.values[column]


def _load_retrain():
    pandas = _module("pandas", read_sql=Mock())
    joblib = _module("joblib", dump=Mock())
    connector = _module("mysql.connector", connect=Mock())
    mysql = _module("mysql")
    mysql.__path__ = []
    mysql.connector = connector
    api = _module("api")
    api.__path__ = [str(AI_ENGINE_ROOT / "api")]
    mysql_config = {
        "host": "database",
        "user": "test_pipeline_user",
        "password": "test-only-password",
        "database": "ai_webhoneypot_db",
    }
    database_config = _module(
        "api.database_config", get_mysql_config=Mock(return_value=mysql_config)
    )

    class _FakeVectorizer:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def fit_transform(self, values):
            return values

    class _FakeClassifier:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def fit(self, features, labels):
            self.labels = labels

    sklearn = _module("sklearn")
    sklearn.__path__ = []
    feature_extraction = _module("sklearn.feature_extraction")
    feature_extraction.__path__ = []
    text = _module(
        "sklearn.feature_extraction.text", TfidfVectorizer=_FakeVectorizer
    )
    ensemble = _module(
        "sklearn.ensemble", RandomForestClassifier=_FakeClassifier
    )
    modules = {
        "pandas": pandas,
        "joblib": joblib,
        "mysql": mysql,
        "mysql.connector": connector,
        "api": api,
        "api.database_config": database_config,
        "sklearn": sklearn,
        "sklearn.feature_extraction": feature_extraction,
        "sklearn.feature_extraction.text": text,
        "sklearn.ensemble": ensemble,
    }
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(
            "ai_engine_test_retrain", AI_ENGINE_ROOT / "retrain.py"
        )
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
    return loaded, connector, pandas, database_config


class RetrainingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (
            cls.module,
            cls.connector,
            cls.pandas,
            cls.database_config,
        ) = _load_retrain()

    def _valid_frame(self):
        return _Frame(
            [f"payload-{index}" for index in range(10)],
            ["SQLi", "XSS"] * 5,
        )

    def test_insufficient_data_fails_without_fabricated_fallback(self):
        frame = _Frame(["only one"], ["SQLi"])
        with self.assertRaisesRegex(ValueError, "At least 10"):
            self.module._validate_training_data(frame)

    def test_invalid_or_single_class_training_data_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "non-empty strings"):
            self.module._validate_training_data(
                _Frame([""] * 10, ["SQLi", "XSS"] * 5)
            )
        with self.assertRaisesRegex(ValueError, "two distinct labels"):
            self.module._validate_training_data(
                _Frame(["payload"] * 10, ["SQLi"] * 10)
            )

    def test_training_uses_database_data_and_creates_new_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            self.module.MODEL_PATH = Path(directory) / "model_rf.pkl"
            self.module.VECTORIZER_PATH = Path(directory) / "vectorizer.pkl"
            self.module.fetch_data_from_db = Mock(return_value=self._valid_frame())

            self.module.retrain_model()

            self.module.fetch_data_from_db.assert_called_once()
            self.module.joblib.dump.assert_has_calls(
                [
                    unittest.mock.call(unittest.mock.ANY, unittest.mock.ANY),
                    unittest.mock.call(unittest.mock.ANY, unittest.mock.ANY),
                ]
            )
            self.assertTrue(self.module.MODEL_PATH.is_file())
            self.assertTrue(self.module.VECTORIZER_PATH.is_file())

    def test_existing_artifact_prevents_retraining_and_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            self.module.MODEL_PATH = Path(directory) / "model_rf.pkl"
            self.module.VECTORIZER_PATH = Path(directory) / "vectorizer.pkl"
            self.module.MODEL_PATH.write_bytes(b"existing model")
            fetch = Mock()
            self.module.fetch_data_from_db = fetch

            with self.assertRaises(FileExistsError):
                self.module.retrain_model()

            self.assertEqual(self.module.MODEL_PATH.read_bytes(), b"existing model")
            fetch.assert_not_called()

    def test_database_fetch_uses_shared_config_and_closes_connection(self):
        connection = Mock()
        self.connector.connect.return_value = connection
        expected = self._valid_frame()
        self.pandas.read_sql.return_value = expected

        result = self.module.fetch_data_from_db()

        self.assertIs(result, expected)
        self.connector.connect.assert_called_once_with(
            **self.database_config.get_mysql_config()
        )
        connection.close.assert_called_once()


class TrainingEntrypointTests(unittest.TestCase):
    def test_legacy_entrypoint_delegates_to_shared_retrainer(self):
        retrain_module = _module("retrain", retrain_model=Mock())
        with patch.dict(sys.modules, {"retrain": retrain_module}):
            spec = importlib.util.spec_from_file_location(
                "ai_engine_test_train_entrypoint",
                AI_ENGINE_ROOT / "models" / "train_model.py",
            )
            loaded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loaded)

        loaded.retrain_model()

        retrain_module.retrain_model.assert_called_once()
