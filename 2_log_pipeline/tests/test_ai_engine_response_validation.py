import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


PIPELINE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE_DIR))

if "requests" not in sys.modules:
    requests_module = types.ModuleType("requests")
    requests_module.post = Mock()
    request_exception = type("RequestException", (Exception,), {})
    requests_module.RequestException = request_exception
    requests_module.Timeout = type("Timeout", (request_exception,), {})
    requests_module.ConnectionError = type("ConnectionError", (request_exception,), {})
    requests_exceptions = types.ModuleType("requests.exceptions")
    requests_exceptions.JSONDecodeError = json.JSONDecodeError
    requests_module.exceptions = requests_exceptions
    sys.modules["requests"] = requests_module
    sys.modules["requests.exceptions"] = requests_exceptions

if "mysql.connector" not in sys.modules:
    mysql_module = types.ModuleType("mysql")
    mysql_module.__path__ = []
    connector_module = types.ModuleType("mysql.connector")
    connector_module.connect = Mock()
    mysql_module.connector = connector_module
    sys.modules["mysql"] = mysql_module
    sys.modules["mysql.connector"] = connector_module

import pipeline_main


class AIEngineResponseValidationTests(unittest.TestCase):
    @staticmethod
    def classified(**overrides):
        response = {
            "source": "Local_ML",
            "classification_status": "CLASSIFIED",
            "attack_type": "SQLi",
            "confidence": 0.85,
            "risk_score": 9,
            "reason": None,
            "recommendation": None,
        }
        response.update(overrides)
        return response

    @staticmethod
    def review(reason, **overrides):
        response = {
            "source": "Local_ML",
            "classification_status": "NEEDS_REVIEW",
            "attack_type": None,
            "confidence": None,
            "risk_score": None,
            "reason": reason,
            "recommendation": None,
        }
        response.update(overrides)
        return response

    def analyze(self, response_data):
        response = Mock(status_code=200)
        response.json.return_value = response_data
        with patch.object(pipeline_main.requests, "post", return_value=response):
            return pipeline_main.analyze_with_ai("synthetic payload")

    def assert_contract_success(self, response_data, status, reason, label, confidence, risk):
        result = self.analyze(response_data)
        self.assertEqual(result.status, pipeline_main.AIStatus.AI_SUCCESS)
        self.assertEqual(result.source, "Local_ML")
        self.assertEqual(
            getattr(result, "classification_status", None),
            status,
        )
        self.assertEqual(getattr(result, "reason", None), reason)
        self.assertEqual(result.attack_type, label)
        self.assertEqual(result.confidence, confidence)
        self.assertEqual(result.risk_score, risk)
        self.assertIsNone(result.recommendation)

    def test_accepts_all_four_contract_outcomes(self):
        self.assert_contract_success(
            self.classified(),
            "CLASSIFIED",
            None,
            "SQLi",
            0.85,
            9,
        )
        self.assert_contract_success(
            self.review("LOW_CONFIDENCE", confidence=0.849999),
            "NEEDS_REVIEW",
            "LOW_CONFIDENCE",
            None,
            0.849999,
            None,
        )
        self.assert_contract_success(
            self.review("MODEL_UNAVAILABLE"),
            "NEEDS_REVIEW",
            "MODEL_UNAVAILABLE",
            None,
            None,
            None,
        )
        self.assert_contract_success(
            self.review("INFERENCE_ERROR"),
            "NEEDS_REVIEW",
            "INFERENCE_ERROR",
            None,
            None,
            None,
        )

    def test_accepts_each_official_label_and_classified_boundaries(self):
        for label, risk in (("Normal", 1), ("SQLi", 9), ("XSS", 10)):
            for confidence in (0.85, 1.0):
                with self.subTest(label=label, confidence=confidence):
                    result = self.analyze(
                        self.classified(
                            attack_type=label,
                            confidence=confidence,
                            risk_score=risk,
                            recommendation="review",
                        )
                    )
                    self.assertEqual(result.status, pipeline_main.AIStatus.AI_SUCCESS)
                    self.assertEqual(
                        getattr(result, "classification_status", None),
                        "CLASSIFIED",
                    )
                    self.assertEqual(result.attack_type, label)
                    self.assertEqual(result.confidence, confidence)
                    self.assertEqual(result.risk_score, risk)
                    self.assertEqual(result.recommendation, "review")

    def test_rejects_gemini_source_and_unknown_taxonomy(self):
        invalid_responses = (
            self.classified(source="Cloud_Gemini_LLM"),
            self.classified(source="Unknown"),
            self.classified(attack_type="RCE"),
            self.classified(attack_type="Suspicious"),
        )
        for response_data in invalid_responses:
            with self.subTest(response=response_data):
                result = self.analyze(response_data)
                self.assertEqual(
                    result.status,
                    pipeline_main.AIStatus.AI_INVALID_RESPONSE,
                )
                self.assertIsNone(result.attack_type)
                self.assertIsNone(result.risk_score)

    def test_rejects_invalid_confidence_types_ranges_and_nonfinite_values(self):
        invalid_values = (
            True,
            False,
            float("nan"),
            float("inf"),
            float("-inf"),
            -0.01,
            1.01,
            0.849999,
        )
        for confidence in invalid_values:
            with self.subTest(confidence=confidence):
                result = self.analyze(self.classified(confidence=confidence))
                self.assertEqual(
                    result.status,
                    pipeline_main.AIStatus.AI_INVALID_RESPONSE,
                )
                self.assertIsNone(result.attack_type)

        invalid_review_confidence = (
            True,
            float("nan"),
            float("inf"),
            float("-inf"),
            -0.01,
            0.85,
        )
        for confidence in invalid_review_confidence:
            with self.subTest(review_confidence=confidence):
                result = self.analyze(
                    self.review("LOW_CONFIDENCE", confidence=confidence)
                )
                self.assertEqual(
                    result.status,
                    pipeline_main.AIStatus.AI_INVALID_RESPONSE,
                )

    def test_rejects_invalid_risk_type_and_range(self):
        for risk_score in (True, False, 0, 11, 1.5, "9"):
            with self.subTest(risk_score=risk_score):
                result = self.analyze(
                    self.classified(risk_score=risk_score)
                )
                self.assertEqual(
                    result.status,
                    pipeline_main.AIStatus.AI_INVALID_RESPONSE,
                )
                self.assertIsNone(result.risk_score)

    def test_rejects_invalid_status_reason_and_field_combinations(self):
        invalid_responses = (
            self.classified(reason="LOW_CONFIDENCE"),
            self.classified(classification_status="UNKNOWN"),
            self.review("UNKNOWN"),
            self.review(None),
            self.review("LOW_CONFIDENCE", confidence=None),
            self.review("LOW_CONFIDENCE", confidence=0.85),
            self.review("LOW_CONFIDENCE", attack_type="Normal"),
            self.review("LOW_CONFIDENCE", risk_score=1),
            self.review("LOW_CONFIDENCE", recommendation="fabricated"),
            self.review("MODEL_UNAVAILABLE", confidence=0.1),
            self.review("INFERENCE_ERROR", confidence=0.1),
            self.review("MODEL_UNAVAILABLE", attack_type="Normal"),
            self.review("INFERENCE_ERROR", risk_score=1),
            self.classified(attack_type=None),
            self.classified(confidence=None),
            self.classified(risk_score=None),
        )
        for response_data in invalid_responses:
            with self.subTest(response=response_data):
                result = self.analyze(response_data)
                self.assertEqual(
                    result.status,
                    pipeline_main.AIStatus.AI_INVALID_RESPONSE,
                )

    def test_requires_every_contract_field_including_nullable_fields(self):
        response_data = self.review("MODEL_UNAVAILABLE")
        for field in tuple(response_data):
            missing = dict(response_data)
            del missing[field]
            with self.subTest(field=field):
                result = self.analyze(missing)
                self.assertEqual(
                    result.status,
                    pipeline_main.AIStatus.AI_INVALID_RESPONSE,
                )


if __name__ == "__main__":
    unittest.main()
