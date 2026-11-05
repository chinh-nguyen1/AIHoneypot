import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


PIPELINE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE_DIR))


def _import_pipeline_main():
    if "requests" not in sys.modules:
        requests_module = types.ModuleType("requests")
        requests_module.post = Mock()
        request_exception = type("RequestException", (Exception,), {})
        requests_module.RequestException = request_exception
        requests_module.Timeout = type("Timeout", (request_exception,), {})
        requests_module.ConnectionError = type(
            "ConnectionError", (request_exception,), {}
        )
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
    return importlib.import_module("pipeline_main")


pipeline_main = _import_pipeline_main()


class AIFailureHandlingTests(unittest.TestCase):
    def setUp(self):
        record = {
            "source_event_id": "0123456789abcdef0123456789abcdef",
            "timestamp": "2026-10-31T12:34:56+00:00",
            "source_ip": "192.0.2.10",
            "method": "POST",
            "uri": "/comment.php",
            "query_params": {},
            "body_params": {"content": "test"},
            "payload": {
                "query_params": {},
                "body_params": {"content": "test"},
            },
            "user_agent": "unit-test",
            "request_type": "web",
        }
        self.raw_record = json.dumps(record, ensure_ascii=False) + "\n"

    @staticmethod
    def _classified(**overrides):
        response = {
            "source": "Local_ML",
            "classification_status": "CLASSIFIED",
            "attack_type": "SQLi",
            "confidence": 0.94,
            "risk_score": 9,
            "reason": None,
            "recommendation": None,
        }
        response.update(overrides)
        return response

    @staticmethod
    def _review(reason, **overrides):
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

    @staticmethod
    def _response(data=None, status_code=200, json_error=None):
        response = Mock()
        response.status_code = status_code
        if json_error is not None:
            response.json.side_effect = json_error
        else:
            response.json.return_value = data
        return response

    def _analyze_response(self, response):
        with patch.object(
            pipeline_main.requests, "post", return_value=response
        ):
            return pipeline_main.analyze_with_ai("payload")

    def _process_failure(self, response_or_error):
        with tempfile.TemporaryDirectory() as state_directory:
            spool_path = Path(state_directory) / "rejected.jsonl"
            with patch.object(
                pipeline_main, "REJECTED_FILE_PATH", str(spool_path)
            ), patch.object(pipeline_main.requests, "post") as ai_post, patch.object(
                pipeline_main.mysql.connector, "connect"
            ) as database_connect, patch.object(
                pipeline_main, "send_email_alert"
            ) as email_alert, patch("builtins.print"):
                if isinstance(response_or_error, BaseException):
                    ai_post.side_effect = response_or_error
                else:
                    ai_post.return_value = response_or_error
                result = pipeline_main.process_line(self.raw_record)

            spool_record = json.loads(spool_path.read_text(encoding="utf-8"))

        database_connect.assert_not_called()
        email_alert.assert_not_called()
        self.assertEqual(spool_record["raw_record"], self.raw_record)
        self.assertEqual(
            spool_record["source_event_id"],
            "0123456789abcdef0123456789abcdef",
        )
        return result, spool_record

    def test_classified_response_is_analysis_only(self):
        response = self._response(self._classified())
        with patch.object(
            pipeline_main.requests, "post", return_value=response
        ), patch.object(
            pipeline_main,
            "parse_record",
            wraps=pipeline_main.parse_record,
        ) as parse, patch.object(
            pipeline_main, "persist_analysis"
        ) as persist, patch.object(
            pipeline_main.mysql.connector, "connect"
        ) as database_connect, patch.object(
            pipeline_main, "send_email_alert"
        ) as email_alert, patch("builtins.print"):
            result = pipeline_main.process_line(self.raw_record)

        self.assertEqual(result.status, pipeline_main.AIStatus.AI_SUCCESS)
        self.assertEqual(
            getattr(result, "classification_status", None),
            "CLASSIFIED",
        )
        self.assertEqual(result.attack_type, "SQLi")
        self.assertEqual(result.confidence, 0.94)
        self.assertEqual(result.risk_score, 9)
        self.assertEqual(result.source, "Local_ML")
        self.assertIsNone(result.reason)
        parse.assert_called_once_with(self.raw_record)
        persist.assert_not_called()
        database_connect.assert_not_called()
        email_alert.assert_not_called()

    def test_all_needs_review_reasons_are_valid_and_not_spooled(self):
        cases = (
            self._review("LOW_CONFIDENCE", confidence=0.849999),
            self._review("MODEL_UNAVAILABLE"),
            self._review("INFERENCE_ERROR"),
        )
        for response_data in cases:
            with self.subTest(reason=response_data["reason"]), tempfile.TemporaryDirectory() as temp:
                spool_path = Path(temp) / "rejected.jsonl"
                with patch.object(
                    pipeline_main, "REJECTED_FILE_PATH", str(spool_path)
                ), patch.object(
                    pipeline_main.requests,
                    "post",
                    return_value=self._response(response_data),
                ), patch.object(
                    pipeline_main.mysql.connector, "connect"
                ) as connect, patch.object(
                    pipeline_main, "send_email_alert"
                ) as alert, patch("builtins.print"):
                    result = pipeline_main.process_line(self.raw_record)

                self.assertEqual(result.status, pipeline_main.AIStatus.AI_SUCCESS)
                self.assertEqual(result.classification_status, "NEEDS_REVIEW")
                self.assertEqual(result.reason, response_data["reason"])
                self.assertIsNone(result.attack_type)
                self.assertIsNone(result.risk_score)
                self.assertIsNone(result.recommendation)
                self.assertFalse(spool_path.exists())
                connect.assert_not_called()
                alert.assert_not_called()

    def test_timeouts_and_connection_failures_are_retryable(self):
        for error in (
            pipeline_main.requests.Timeout("timeout"),
            pipeline_main.requests.ConnectionError("connection refused"),
        ):
            with self.subTest(error=type(error).__name__):
                result, spool_record = self._process_failure(error)
                self.assertEqual(result.status, pipeline_main.AIStatus.AI_RETRY)
                self.assertEqual(spool_record["status"], "AI_RETRY")

    def test_http_statuses_follow_approved_retry_policy(self):
        status_expectations = {
            408: pipeline_main.AIStatus.AI_RETRY,
            425: pipeline_main.AIStatus.AI_RETRY,
            429: pipeline_main.AIStatus.AI_RETRY,
            500: pipeline_main.AIStatus.AI_RETRY,
            501: pipeline_main.AIStatus.AI_RETRY,
            502: pipeline_main.AIStatus.AI_RETRY,
            503: pipeline_main.AIStatus.AI_RETRY,
            504: pipeline_main.AIStatus.AI_RETRY,
            400: pipeline_main.AIStatus.AI_INVALID_RESPONSE,
            401: pipeline_main.AIStatus.AI_INVALID_RESPONSE,
            403: pipeline_main.AIStatus.AI_INVALID_RESPONSE,
            404: pipeline_main.AIStatus.AI_INVALID_RESPONSE,
            422: pipeline_main.AIStatus.AI_INVALID_RESPONSE,
        }
        for status_code, expected_status in status_expectations.items():
            with self.subTest(status_code=status_code):
                result = self._analyze_response(
                    self._response(status_code=status_code)
                )
                self.assertEqual(result.status, expected_status)

    def test_rejects_malformed_json_and_missing_contract_fields(self):
        cases = (
            self._response(json_error=ValueError("not JSON")),
            self._response(
                {
                    key: value
                    for key, value in self._classified().items()
                    if key != "recommendation"
                }
            ),
            self._response([]),
            self._response("not an object"),
        )
        for response in cases:
            with self.subTest(response=response):
                result = self._analyze_response(response)
                self.assertEqual(
                    result.status,
                    pipeline_main.AIStatus.AI_INVALID_RESPONSE,
                )

    def test_invalid_taxonomy_source_and_numeric_fields_are_not_classified(self):
        invalid_responses = (
            self._classified(source="Cloud_Gemini_LLM"),
            self._classified(attack_type="RCE"),
            self._classified(confidence=True),
            self._classified(confidence=float("nan")),
            self._classified(confidence=float("inf")),
            self._classified(confidence=0.849999),
            self._classified(risk_score=True),
            self._classified(risk_score=0),
            self._classified(risk_score=11),
            self._classified(recommendation={"text": "invalid"}),
            self._review("LOW_CONFIDENCE", confidence=True),
            self._review("LOW_CONFIDENCE", confidence=0.85),
            self._review("MODEL_UNAVAILABLE", confidence=0.1),
            self._review("INFERENCE_ERROR", risk_score=1),
            self._review("UNKNOWN"),
            self._review("LOW_CONFIDENCE", attack_type="Normal"),
            self._review("LOW_CONFIDENCE", risk_score=1),
            self._review("LOW_CONFIDENCE", recommendation="fabricated"),
        )
        for response_data in invalid_responses:
            with self.subTest(response=response_data):
                result = self._analyze_response(self._response(response_data))
                self.assertEqual(
                    result.status,
                    pipeline_main.AIStatus.AI_INVALID_RESPONSE,
                )
                self.assertIsNone(result.attack_type)
                self.assertIsNone(result.risk_score)

    def test_failed_response_does_not_log_request_payload(self):
        response = self._response(self._classified(source="Unknown"))
        with patch.object(
            pipeline_main.requests, "post", return_value=response
        ), patch("builtins.print") as log:
            result = pipeline_main.analyze_with_ai("sensitive request payload")

        self.assertEqual(result.status, pipeline_main.AIStatus.AI_INVALID_RESPONSE)
        self.assertNotIn("sensitive request payload", str(log.call_args_list))


if __name__ == "__main__":
    unittest.main()
