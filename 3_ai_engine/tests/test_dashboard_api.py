import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


AI_ENGINE_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = AI_ENGINE_ROOT / "api" / "main.py"


class _HTTPException(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _FastAPI:
    def __init__(self, **kwargs):
        pass

    def add_middleware(self, *args, **kwargs):
        pass

    def get(self, *args, **kwargs):
        return lambda function: function

    def post(self, *args, **kwargs):
        return lambda function: function


class _PayloadRequest:
    def __init__(self, payload):
        self.payload = payload


def _load_main():
    fastapi = types.ModuleType("fastapi")
    fastapi.FastAPI = _FastAPI
    fastapi.HTTPException = _HTTPException
    cors_module = types.ModuleType("fastapi.middleware.cors")
    cors_module.CORSMiddleware = object()
    middleware_module = types.ModuleType("fastapi.middleware")
    middleware_module.__path__ = []
    pydantic = types.ModuleType("pydantic")
    pydantic.BaseModel = object
    joblib = types.ModuleType("joblib")
    joblib.load = Mock(side_effect=FileNotFoundError)
    connector = types.ModuleType("mysql.connector")
    connector.connect = Mock()
    mysql = types.ModuleType("mysql")
    mysql.__path__ = []
    mysql.connector = connector
    api = types.ModuleType("api")
    api.__path__ = [str(AI_ENGINE_ROOT / "api")]
    advisor = types.ModuleType("api.llm_advisor")
    advisor.LLMInvalidResponseError = type(
        "LLMInvalidResponseError", (ValueError,), {}
    )
    advisor.analyze_and_advise_with_llm = Mock()
    database_config = types.ModuleType("api.database_config")
    database_config.get_mysql_config = Mock(return_value={})
    model_loader = types.ModuleType("api.model_loader")
    model_loader_error = type("ModelLoaderError", (RuntimeError,), {})
    model_loader.ModelLoaderError = model_loader_error
    model_loader.load_model_release = Mock(
        side_effect=model_loader_error("test model unavailable")
    )

    modules = {
        "fastapi": fastapi,
        "fastapi.middleware": middleware_module,
        "fastapi.middleware.cors": cors_module,
        "pydantic": pydantic,
        "joblib": joblib,
        "mysql": mysql,
        "mysql.connector": connector,
        "api": api,
        "api.llm_advisor": advisor,
        "api.database_config": database_config,
        "api.model_loader": model_loader,
    }
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(
            "dashboard_api_test_main", MAIN_PATH
        )
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
    loaded.PayloadRequest = _PayloadRequest
    return loaded, connector


class DashboardApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module, cls.connector = _load_main()

    def setUp(self):
        self.connector.connect.reset_mock(return_value=True, side_effect=True)
        self.connection = Mock()
        self.cursor = Mock()
        self.connection.cursor.return_value = self.cursor
        self.connector.connect.return_value = self.connection

    def test_stats_count_latest_classifications_and_unclassified_events(self):
        self.cursor.fetchone.side_effect = [
            {"total_logs": 7, "unique_ips": 4},
            {"unclassified_count": 2},
        ]
        self.cursor.fetchall.return_value = [
            {"attack_type": "SQLi", "event_count": 2},
            {"attack_type": "XSS", "event_count": 1},
            {"attack_type": "Normal", "event_count": 1},
            {"attack_type": "Suspicious", "event_count": 1},
        ]

        result = self.module.get_stats()

        self.assertEqual(result["total_logs"], 7)
        self.assertEqual(result["classified_count"], 5)
        self.assertEqual(result["unclassified_count"], 2)
        self.assertEqual(result["attack_counts"]["Suspicious"], 1)
        self.assertEqual(result["normal_count"], 1)
        self.assertEqual(result["unique_ips"], 4)
        self.assertEqual(self.cursor.execute.call_count, 3)
        self.assertIn("ORDER BY a2.analyzed_at DESC, a2.id DESC", self.cursor.execute.call_args_list[1].args[0])
        self.cursor.close.assert_called_once()
        self.connection.close.assert_called_once()

    def test_logs_use_stable_id_and_return_nullable_classification_fields(self):
        expected = [
            {
                "id": "41",
                "created_at": "2026-11-01 11:00:00",
                "client_ip": "192.0.2.3",
                "http_method": "GET",
                "request_uri": "/",
                "attack_type": "SQLi",
                "confidence_score": 0.9,
                "risk_score": 9,
                "classification_status": "CLASSIFIED",
                "recommendation": "Use parameterized SQL statements.",
            },
            {
                "id": "40",
                "created_at": "2026-11-01 10:59:00",
                "client_ip": "192.0.2.4",
                "http_method": "POST",
                "request_uri": "/login",
                "attack_type": None,
                "confidence_score": None,
                "risk_score": None,
                "classification_status": "UNCLASSIFIED",
                "recommendation": None,
            },
        ]
        self.cursor.fetchall.return_value = expected

        result = self.module.get_live_logs()

        self.assertEqual(result, expected)
        query = self.cursor.execute.call_args.args[0]
        self.assertIn("LEFT JOIN ai_labels", query)
        self.assertIn("ORDER BY r.id DESC", query)
        self.assertIn("a.confidence_score", query)
        self.assertIn("UNCLASSIFIED", query)
        self.assertIn("a.recommendation AS recommendation", query)
        self.assertEqual(
            result[0]["recommendation"],
            "Use parameterized SQL statements.",
        )
        self.assertIsNone(result[1]["recommendation"])
        self.cursor.close.assert_called_once()
        self.connection.close.assert_called_once()

    def test_statistics_database_failure_returns_http_error(self):
        self.connector.connect.side_effect = OSError("database unavailable")

        with self.assertRaises(_HTTPException) as raised:
            self.module.get_stats()

        self.assertEqual(raised.exception.status_code, 503)
        self.assertNotEqual(raised.exception.detail, {
            "total_logs": 0,
            "sqli_count": 0,
            "xss_count": 0,
            "normal_count": 0,
            "unique_ips": 0,
        })

    def test_live_log_database_failure_returns_http_error(self):
        self.cursor.execute.side_effect = OSError("query failed")

        with self.assertRaises(_HTTPException) as raised:
            self.module.get_live_logs()

        self.assertEqual(raised.exception.status_code, 503)
        self.connection.close.assert_called_once()
        self.cursor.close.assert_called_once()

    def test_database_cleanup_attempts_both_resources_on_close_error(self):
        self.cursor.close.side_effect = OSError("cursor close failed")

        with self.assertRaises(_HTTPException) as raised:
            self.module.get_live_logs()

        self.assertEqual(raised.exception.status_code, 503)
        self.connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
