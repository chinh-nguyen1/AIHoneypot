import math
import unittest

from pydantic import ValidationError

from api.schemas import AnalysisResponse


class AnalysisResponseSchemaTests(unittest.TestCase):
    def classified(self, **overrides):
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

    def review(self, reason, **overrides):
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

    def test_accepts_all_four_contract_outcomes(self):
        cases = (
            self.classified(),
            self.review(
                "LOW_CONFIDENCE",
                confidence=0.849999,
            ),
            self.review("MODEL_UNAVAILABLE"),
            self.review("INFERENCE_ERROR"),
        )
        for response in cases:
            with self.subTest(response=response):
                validated = AnalysisResponse.model_validate(response)
                self.assertEqual(validated.model_dump(), response)

    def test_accepts_each_official_label_and_risk_boundaries(self):
        for label, risk in (("Normal", 1), ("SQLi", 9), ("XSS", 10)):
            with self.subTest(label=label):
                validated = AnalysisResponse.model_validate(
                    self.classified(
                        attack_type=label,
                        confidence=1.0,
                        risk_score=risk,
                    )
                )
                self.assertEqual(validated.attack_type, label)

    def test_confidence_threshold_boundary_is_exact(self):
        classified = AnalysisResponse.model_validate(
            self.classified(confidence=0.85)
        )
        low_confidence = AnalysisResponse.model_validate(
            self.review("LOW_CONFIDENCE", confidence=0.0)
        )
        self.assertEqual(classified.confidence, 0.85)
        self.assertEqual(low_confidence.confidence, 0.0)

        invalid_cases = (
            self.classified(confidence=0.849999),
            self.review("LOW_CONFIDENCE", confidence=0.85),
        )
        for response in invalid_cases:
            with self.subTest(response=response):
                with self.assertRaises(ValidationError):
                    AnalysisResponse.model_validate(response)

    def test_rejects_nonfinite_boolean_and_out_of_range_confidence(self):
        invalid_values = (
            float("nan"),
            float("inf"),
            float("-inf"),
            True,
            -0.0001,
            1.0001,
        )
        for confidence in invalid_values:
            with self.subTest(confidence=confidence):
                with self.assertRaises(ValidationError):
                    AnalysisResponse.model_validate(
                        self.classified(confidence=confidence)
                    )
                if isinstance(confidence, (int, float)) and not isinstance(
                    confidence, bool
                ) and math.isfinite(confidence) and 0 <= confidence < 0.85:
                    continue
                with self.assertRaises(ValidationError):
                    AnalysisResponse.model_validate(
                        self.review("LOW_CONFIDENCE", confidence=confidence)
                    )

    def test_rejects_invalid_risk_values(self):
        for risk_score in (0, 11, True, 1.5):
            with self.subTest(risk_score=risk_score):
                with self.assertRaises(ValidationError):
                    AnalysisResponse.model_validate(
                        self.classified(risk_score=risk_score)
                    )

    def test_rejects_invalid_taxonomy_and_source(self):
        for label in ("Unknown", "Suspicious", None):
            with self.subTest(label=label):
                with self.assertRaises(ValidationError):
                    AnalysisResponse.model_validate(
                        self.classified(attack_type=label)
                    )
        with self.assertRaises(ValidationError):
            AnalysisResponse.model_validate(self.classified(source="Cloud_Gemini_LLM"))

    def test_recommendation_is_string_or_null(self):
        response = self.classified(recommendation="Use parameterized queries.")
        self.assertEqual(
            AnalysisResponse.model_validate(response).recommendation,
            "Use parameterized queries.",
        )
        with self.assertRaises(ValidationError):
            AnalysisResponse.model_validate(self.classified(recommendation=123))

    def test_json_schema_publishes_numeric_bounds(self):
        properties = AnalysisResponse.model_json_schema()["properties"]
        confidence_number = next(
            item
            for item in properties["confidence"]["anyOf"]
            if item.get("type") == "number"
        )
        risk_integer = next(
            item
            for item in properties["risk_score"]["anyOf"]
            if item.get("type") == "integer"
        )
        self.assertEqual(confidence_number["minimum"], 0)
        self.assertEqual(confidence_number["maximum"], 1)
        self.assertEqual(risk_integer["minimum"], 1)
        self.assertEqual(risk_integer["maximum"], 10)

    def test_rejects_invalid_status_reason_and_field_combinations(self):
        invalid_cases = (
            self.classified(reason="LOW_CONFIDENCE"),
            self.classified(classification_status="UNKNOWN"),
            self.review("UNKNOWN"),
            self.review(None),
            self.review("LOW_CONFIDENCE", confidence=None),
            self.review("MODEL_UNAVAILABLE", confidence=0.1),
            self.review("INFERENCE_ERROR", confidence=0.1),
            self.review("LOW_CONFIDENCE", attack_type="Normal"),
            self.review("LOW_CONFIDENCE", risk_score=1),
            self.review("LOW_CONFIDENCE", recommendation="invented"),
        )
        for response in invalid_cases:
            with self.subTest(response=response):
                with self.assertRaises(ValidationError):
                    AnalysisResponse.model_validate(response)

    def test_all_response_fields_are_required_but_nullable(self):
        response = self.review("MODEL_UNAVAILABLE")
        for field in response:
            missing = dict(response)
            del missing[field]
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    AnalysisResponse.model_validate(missing)

        validated = AnalysisResponse.model_validate(response)
        self.assertIsNone(validated.attack_type)
        self.assertIsNone(validated.confidence)
        self.assertIsNone(validated.risk_score)
        self.assertIsNone(validated.recommendation)


if __name__ == "__main__":
    unittest.main()
