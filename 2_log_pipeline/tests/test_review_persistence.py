import hashlib
import os
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


PIPELINE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE_DIR))

if "requests" not in sys.modules:
    requests_module = types.ModuleType("requests")
    requests_module.post = mock.Mock()
    request_exception = type("RequestException", (Exception,), {})
    requests_module.RequestException = request_exception
    requests_module.Timeout = type("Timeout", (request_exception,), {})
    requests_module.ConnectionError = type("ConnectionError", (request_exception,), {})
    requests_exceptions = types.ModuleType("requests.exceptions")
    requests_exceptions.JSONDecodeError = ValueError
    requests_module.exceptions = requests_exceptions
    sys.modules["requests"] = requests_module
    sys.modules["requests.exceptions"] = requests_exceptions

if "mysql.connector" not in sys.modules:
    mysql_module = types.ModuleType("mysql")
    mysql_module.__path__ = []
    connector_module = types.ModuleType("mysql.connector")
    connector_module.connect = mock.Mock()
    mysql_module.connector = connector_module
    sys.modules["mysql"] = mysql_module
    sys.modules["mysql.connector"] = connector_module

import pipeline_main
from inbox_record import create_inbox_event, create_ingestion_key


class ReviewPersistenceTests(unittest.TestCase):
    @staticmethod
    def raw_record_bytes():
        return (
            b'{"source_event_id":"0123456789abcdef0123456789abcdef",'
            b'"timestamp":"2026-10-31T12:34:56+00:00",'
            b'"source_ip":"192.0.2.10","method":"POST",'
            b'"uri":"/comment.php","query_params":{},'
            b'"body_params":{"content":"synthetic"},'
            b'"payload":{"query_params":{},"body_params":{"content":"synthetic"}},'
            b'"user_agent":"persistence-test","request_type":"web"}\n'
        )

    def setUp(self):
        self.raw_bytes = self.raw_record_bytes()
        self.event = pipeline_main.parse_record(self.raw_bytes.decode("utf-8"))
        self.generation = "review-persistence-test"
        self.ingestion_key = create_ingestion_key(
            self.generation,
            0,
            len(self.raw_bytes),
        )
        self.inbox_event = create_inbox_event(
            self.generation,
            0,
            len(self.raw_bytes),
            self.raw_bytes,
        )

    @staticmethod
    def analysis(
        status,
        reason=None,
        confidence=None,
        attack_type=None,
        risk_score=None,
        recommendation=None,
    ):
        return SimpleNamespace(
            status=pipeline_main.AIStatus.AI_SUCCESS,
            classification_status=status,
            attack_type=attack_type,
            confidence=confidence,
            risk_score=risk_score,
            reason=reason,
            source="Local_ML",
            recommendation=recommendation,
        )

    def persist(self, analysis, connection):
        cursor = connection.cursor.return_value
        cursor.lastrowid = 77
        with mock.patch.object(
            pipeline_main,
            "lookup_persisted_event",
            return_value=None,
        ), mock.patch.object(
            pipeline_main.mysql.connector,
            "connect",
            return_value=connection,
        ), mock.patch.dict(
            os.environ,
            {"MYSQL_PASSWORD": "test-only-password"},
        ), mock.patch.object(
            pipeline_main,
            "send_email_alert",
        ) as alert, mock.patch("builtins.print"):
            result = pipeline_main.persist_analysis(
                self.event,
                analysis,
                inbox_event=self.inbox_event,
            )
        return result, cursor, alert

    def test_classified_high_risk_persists_one_outcome_and_eligible_pentest(self):
        connection = mock.Mock()
        analysis = self.analysis(
            "CLASSIFIED",
            confidence=0.93,
            attack_type="SQLi",
            risk_score=9,
            recommendation="Use parameterized SQL statements.",
        )

        result, cursor, alert = self.persist(analysis, connection)

        self.assertEqual(
            result.status,
            pipeline_main.DatabasePersistenceStatus.DB_SUCCESS,
        )
        self.assertEqual(cursor.execute.call_count, 3)
        outcome_sql, outcome_parameters = cursor.execute.call_args_list[1].args
        self.assertIn("classification_status", outcome_sql)
        self.assertIn("classification_reason", outcome_sql)
        self.assertEqual(
            outcome_parameters,
            (
                77,
                "CLASSIFIED",
                "SQLi",
                0.93,
                9,
                "Local_ML",
                None,
                "Use parameterized SQL statements.",
            ),
        )
        pentest_sql, pentest_parameters = cursor.execute.call_args_list[2].args
        self.assertIn("INSERT INTO pentest_payloads", pentest_sql)
        self.assertEqual(pentest_parameters[1], "SQLi")
        connection.commit.assert_called_once()
        alert.assert_called_once()

    def test_all_review_reasons_persist_without_labels_scores_or_pentest(self):
        review_cases = (
            ("LOW_CONFIDENCE", 0.4),
            ("MODEL_UNAVAILABLE", None),
            ("INFERENCE_ERROR", None),
        )
        for reason, confidence in review_cases:
            with self.subTest(reason=reason):
                connection = mock.Mock()
                analysis = self.analysis(
                    "NEEDS_REVIEW",
                    reason=reason,
                    confidence=confidence,
                )

                result, cursor, alert = self.persist(analysis, connection)

                self.assertEqual(
                    result.status,
                    pipeline_main.DatabasePersistenceStatus.DB_SUCCESS,
                )
                self.assertEqual(cursor.execute.call_count, 2)
                outcome_sql, outcome_parameters = cursor.execute.call_args_list[1].args
                self.assertIn("classification_status", outcome_sql)
                self.assertIn("classification_reason", outcome_sql)
                self.assertEqual(
                    outcome_parameters,
                    (
                        77,
                        "NEEDS_REVIEW",
                        None,
                        confidence,
                        None,
                        "Local_ML",
                        reason,
                        None,
                    ),
                )
                self.assertNotIn(
                    "INSERT INTO pentest_payloads",
                    " ".join(call.args[0] for call in cursor.execute.call_args_list),
                )
                connection.commit.assert_called_once()
                alert.assert_not_called()
                self.assertIsNone(analysis.attack_type)
                self.assertIsNone(analysis.risk_score)

    def test_database_failure_does_not_confirm_review_persistence(self):
        connection = mock.Mock()
        cursor = connection.cursor.return_value
        cursor.lastrowid = 77
        cursor.execute.side_effect = [None, OSError("synthetic insert failure")]
        analysis = self.analysis(
            "NEEDS_REVIEW",
            reason="MODEL_UNAVAILABLE",
        )

        with mock.patch.object(
            pipeline_main,
            "lookup_persisted_event",
            return_value=None,
        ), mock.patch.object(
            pipeline_main.mysql.connector,
            "connect",
            return_value=connection,
        ), mock.patch.dict(
            os.environ,
            {"MYSQL_PASSWORD": "test-only-password"},
        ), mock.patch.object(
            pipeline_main,
            "send_email_alert",
        ), mock.patch("builtins.print"):
            with self.assertRaises(pipeline_main.DatabasePersistenceError):
                pipeline_main.persist_analysis(
                    self.event,
                    analysis,
                    inbox_event=self.inbox_event,
                )

        connection.commit.assert_not_called()
        connection.rollback.assert_called_once()
        cursor.close.assert_called_once()
        connection.close.assert_called_once()

    def test_duplicate_or_conflicting_analysis_does_not_overwrite_existing_outcome(self):
        existing = pipeline_main.DatabasePersistenceResult(
            status=pipeline_main.DatabasePersistenceStatus.DB_SUCCESS,
            log_id=77,
            already_persisted=True,
        )
        conflicting_analysis = self.analysis(
            "CLASSIFIED",
            confidence=0.99,
            attack_type="XSS",
            risk_score=10,
        )
        with mock.patch.object(
            pipeline_main,
            "lookup_persisted_event",
            return_value=existing,
        ) as lookup, mock.patch.object(
            pipeline_main.mysql.connector,
            "connect",
        ) as connect:
            result = pipeline_main.persist_analysis(
                self.event,
                conflicting_analysis,
                inbox_event=self.inbox_event,
            )

        self.assertIs(result, existing)
        lookup.assert_called_once_with(self.event, self.inbox_event)
        connect.assert_not_called()

    def test_source_hash_mismatch_remains_fail_closed(self):
        wrong_hash = bytes.fromhex("0" * 64)
        self.assertNotEqual(
            wrong_hash,
            hashlib.sha256(self.raw_bytes).digest(),
        )
        connection = mock.Mock()
        cursor = connection.cursor.return_value
        cursor.fetchone.return_value = (
            77,
            self.ingestion_key.encode("utf-8"),
            wrong_hash,
        )
        with mock.patch.object(
            pipeline_main.mysql.connector,
            "connect",
            return_value=connection,
        ), mock.patch.object(
            pipeline_main,
            "get_mysql_config",
            return_value={"host": "synthetic"},
        ):
            with self.assertRaises(pipeline_main.IdempotencyIntegrityError):
                pipeline_main.persist_analysis(
                    self.event,
                    self.analysis(
                        "CLASSIFIED",
                        confidence=0.9,
                        attack_type="SQLi",
                        risk_score=7,
                    ),
                    inbox_event=self.inbox_event,
                )
        self.assertEqual(cursor.execute.call_count, 1)
        cursor.fetchall.assert_not_called()
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
