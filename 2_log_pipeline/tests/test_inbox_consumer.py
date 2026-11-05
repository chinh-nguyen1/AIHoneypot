import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
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
    connector_module.connect = mock.Mock()
    mysql_module.connector = connector_module
    sys.modules["mysql"] = mysql_module
    sys.modules["mysql.connector"] = connector_module

import inbox_consumer
import inbox_writer
import pipeline_main
from durability_contract import (
    DirectorySyncState,
    DurabilityError,
    PublicationState,
)
from inbox_consumer import (
    CONSUMER_STATE_VERSION,
    ConsumerIntegrityError,
    ConsumerStateError,
    ConsumerStatus,
    InboxConsumer,
)
from inbox_record import (
    create_ingestion_key,
    create_inbox_event,
    inbox_event_filename,
)


_TRY_LOCK_SCRIPT = """
import sys
sys.path.insert(0, sys.argv[1])
from single_writer_lock import LockContentionError, SingleWriterLock
try:
    with SingleWriterLock(
        sys.argv[2], checkpoint_path=sys.argv[3], inbox_directory=sys.argv[4]
    ):
        print("ACQUIRED", flush=True)
except LockContentionError:
    print("CONTENDED", flush=True)
"""


class InboxConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.inbox_directory = self.root / "inbox"
        self.inbox_directory.mkdir()
        self.state_directory = self.root / "state" / "consumer"
        self.state_directory.mkdir(parents=True)
        self.checkpoint_path = self.root / "state" / "checkpoint.json"
        self.lock_path = self.root / "state" / "consumer.lock"
        self.now = datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc)
        self.enterContext(
            mock.patch.object(inbox_writer, "_fsync_directory")
        )
        self.enterContext(
            mock.patch.object(inbox_consumer, "_fsync_directory")
        )
        self.enterContext(
            mock.patch.object(
                inbox_consumer,
                "lookup_persisted_event",
                return_value=None,
            )
        )
        self.event = self.create_event(self.valid_record())
        self.publish(self.event)

    @staticmethod
    def valid_record():
        return (
            json.dumps(
                {
                    "source_event_id": "0123456789abcdef0123456789abcdef",
                    "timestamp": "2026-10-31T12:34:56+00:00",
                    "source_ip": "192.0.2.10",
                    "method": "POST",
                    "uri": "/comment.php",
                    "query_params": {"q": "value"},
                    "body_params": {"content": "payload"},
                    "payload": {
                        "query_params": {"q": "value"},
                        "body_params": {"content": "payload"},
                    },
                    "user_agent": "consumer-test",
                    "request_type": "web",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    def create_event(self, raw_bytes, start=0, generation="fixture-generation"):
        return create_inbox_event(
            generation,
            start,
            start + len(raw_bytes),
            raw_bytes,
            source_event_id="0123456789abcdef0123456789abcdef",
            ingested_at="2026-11-01T03:00:00+00:00",
        )

    def publish(self, event):
        return inbox_writer.write_inbox_event(self.inbox_directory, event)

    def make_consumer(self, **kwargs):
        return InboxConsumer(
            inbox_directory=self.inbox_directory,
            state_directory=self.state_directory,
            lock_path=self.lock_path,
            checkpoint_path=self.checkpoint_path,
            clock=lambda: self.now,
            sleeper=lambda _: None,
            retry_base_seconds=2,
            retry_max_seconds=8,
            **kwargs,
        )

    def analysis_result(self, status=pipeline_main.AIStatus.AI_SUCCESS):
        if status is pipeline_main.AIStatus.AI_SUCCESS:
            return pipeline_main.AIAnalysisResult(
                status=status,
                attack_type="SQLi",
                confidence=0.93,
                risk_score=7,
                source="Local_ML",
            )
        return pipeline_main.AIAnalysisResult(
            status=status,
            failure_reason="test failure",
        )

    def successful_persistence(self):
        return pipeline_main.DatabasePersistenceResult(
            status=pipeline_main.DatabasePersistenceStatus.DB_SUCCESS,
            log_id=51,
        )

    def already_persisted_result(self):
        return pipeline_main.DatabasePersistenceResult(
            status=pipeline_main.DatabasePersistenceStatus.DB_SUCCESS,
            log_id=51,
            already_persisted=True,
            recommendation="Stored recommendation.",
        )

    def status_path(self, ingestion_key=None):
        key = ingestion_key or self.event.ingestion_key
        return self.state_directory / (
            hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json"
        )

    def read_state(self, ingestion_key=None):
        return json.loads(self.status_path(ingestion_key).read_text(encoding="utf-8"))

    def run_with_successful_ai_and_db(self, consumer=None):
        consumer = consumer or self.make_consumer()
        with mock.patch.object(
            inbox_consumer, "analyze_event", return_value=self.analysis_result()
        ) as analyze, mock.patch.object(
            inbox_consumer,
            "persist_analysis",
            return_value=self.successful_persistence(),
        ) as persist:
            summary = consumer.consume_once()
        return summary, analyze, persist

    def test_consumes_valid_event_and_persists_processed_sidecar(self):
        summary, analyze, persist = self.run_with_successful_ai_and_db()

        self.assertEqual(summary.processed, 1)
        analyze.assert_called_once()
        persist.assert_called_once()
        persisted_event, persisted_analysis = persist.call_args.args
        self.assertEqual(persisted_event.raw_record, self.event.raw_record)
        self.assertEqual(persisted_analysis, analyze.return_value)
        self.assertEqual(
            persist.call_args.kwargs,
            {"inbox_event": self.event},
        )
        state = self.read_state()
        self.assertEqual(state["version"], CONSUMER_STATE_VERSION)
        self.assertEqual(state["ingestion_key"], self.event.ingestion_key)
        self.assertEqual(state["status"], ConsumerStatus.PROCESSED.value)
        self.assertEqual(state["retry_count"], 0)
        self.assertIsNone(state["reason"])
        self.assertTrue(self.inbox_directory.joinpath(
            inbox_event_filename(self.event.ingestion_key)
        ).exists())

    def test_needs_review_is_persisted_before_processed_sidecar(self):
        review = SimpleNamespace(
            status=pipeline_main.AIStatus.AI_SUCCESS,
            classification_status="NEEDS_REVIEW",
            attack_type=None,
            confidence=0.4,
            risk_score=None,
            reason="LOW_CONFIDENCE",
            source="Local_ML",
            recommendation=None,
        )
        with mock.patch.object(
            inbox_consumer, "analyze_event", return_value=review
        ) as analyze, mock.patch.object(
            inbox_consumer,
            "persist_analysis",
            return_value=self.successful_persistence(),
        ) as persist:
            summary = self.make_consumer().consume_once()

        self.assertEqual(summary.processed, 1)
        analyze.assert_called_once()
        persist.assert_called_once_with(
            pipeline_main.parse_record(self.event.raw_record),
            review,
            inbox_event=self.event,
        )
        self.assertIsNone(review.attack_type)
        self.assertIsNone(review.risk_score)
        self.assertEqual(
            self.read_state()["status"],
            ConsumerStatus.PROCESSED.value,
        )

    def test_needs_review_database_failure_is_retryable_not_processed(self):
        review = SimpleNamespace(
            status=pipeline_main.AIStatus.AI_SUCCESS,
            classification_status="NEEDS_REVIEW",
            attack_type=None,
            confidence=None,
            risk_score=None,
            reason="MODEL_UNAVAILABLE",
            source="Local_ML",
            recommendation=None,
        )
        with mock.patch.object(
            inbox_consumer, "analyze_event", return_value=review
        ), mock.patch.object(
            inbox_consumer,
            "persist_analysis",
            side_effect=pipeline_main.DatabasePersistenceError(
                "db unavailable", OSError("db unavailable")
            ),
        ):
            summary = self.make_consumer().consume_once()

        self.assertEqual(summary.retried, 1)
        self.assertEqual(
            self.read_state()["status"],
            ConsumerStatus.RETRY.value,
        )
        self.assertTrue(
            self.inbox_directory.joinpath(
                inbox_event_filename(self.event.ingestion_key)
            ).is_file()
        )

    def test_processed_event_is_skipped_when_consumer_restarts(self):
        self.run_with_successful_ai_and_db()

        with mock.patch.object(inbox_consumer, "analyze_event") as analyze, mock.patch.object(
            inbox_consumer, "persist_analysis"
        ) as persist:
            summary = self.make_consumer().consume_once()

        self.assertEqual(summary.skipped, 1)
        analyze.assert_not_called()
        persist.assert_not_called()

    def test_database_match_marks_processed_without_ai_or_insert(self):
        persisted = self.already_persisted_result()
        with mock.patch.object(
            inbox_consumer,
            "lookup_persisted_event",
            return_value=persisted,
        ) as lookup, mock.patch.object(
            inbox_consumer, "analyze_event"
        ) as analyze, mock.patch.object(
            inbox_consumer, "persist_analysis"
        ) as persist:
            summary = self.make_consumer().consume_once()

        self.assertEqual(summary.processed, 1)
        lookup.assert_called_once_with(
            pipeline_main.parse_record(self.event.raw_record),
            self.event,
        )
        analyze.assert_not_called()
        persist.assert_not_called()
        self.assertEqual(persisted.recommendation, "Stored recommendation.")
        self.assertEqual(self.read_state()["status"], ConsumerStatus.PROCESSED.value)

    def test_database_integrity_conflict_is_rejected_without_processing(self):
        with mock.patch.object(
            inbox_consumer,
            "lookup_persisted_event",
            side_effect=pipeline_main.IdempotencyIntegrityError("source mismatch"),
        ), mock.patch.object(inbox_consumer, "analyze_event") as analyze:
            summary = self.make_consumer().consume_once()

        self.assertEqual(summary.rejected, 1)
        analyze.assert_not_called()
        self.assertEqual(self.read_state()["status"], ConsumerStatus.REJECTED.value)
        self.assertIn("integrity conflict", self.read_state()["reason"])
        self.assertTrue(
            self.inbox_directory.joinpath(
                inbox_event_filename(self.event.ingestion_key)
            ).is_file()
        )

    def test_database_precheck_failure_creates_retry_before_ai(self):
        failure = pipeline_main.DatabasePersistenceError(
            "database unavailable", OSError("database unavailable")
        )
        with mock.patch.object(
            inbox_consumer,
            "lookup_persisted_event",
            side_effect=failure,
        ), mock.patch.object(inbox_consumer, "analyze_event") as analyze:
            summary = self.make_consumer().consume_once()

        self.assertEqual(summary.retried, 1)
        analyze.assert_not_called()
        self.assertEqual(self.read_state()["status"], ConsumerStatus.RETRY.value)

    def test_valid_event_is_prechecked_before_ai_and_persistence(self):
        order = []
        with mock.patch.object(
            inbox_consumer,
            "lookup_persisted_event",
            side_effect=lambda *_: order.append("precheck"),
        ), mock.patch.object(
            inbox_consumer,
            "analyze_event",
            side_effect=lambda _: order.append("ai") or self.analysis_result(),
        ), mock.patch.object(
            inbox_consumer,
            "persist_analysis",
            side_effect=lambda *_args, **_kwargs: (
                order.append("persist") or self.successful_persistence()
            ),
        ):
            summary = self.make_consumer().consume_once()

        self.assertEqual(summary.processed, 1)
        self.assertEqual(order, ["precheck", "ai", "persist"])

    def test_invalid_inbox_metadata_is_rejected_before_ai_or_persistence(self):
        invalid_events = (
            replace(self.event, ingestion_key="not-json"),
            replace(self.event, source_file_identity="different-generation"),
            replace(self.event, byte_offset_end=self.event.byte_offset_end + 1),
            replace(self.event, raw_bytes_sha256="0" * 64),
        )
        for invalid_event in invalid_events:
            with self.subTest(ingestion_key=invalid_event.ingestion_key):
                with mock.patch.object(
                    inbox_consumer, "analyze_event"
                ) as analyze, mock.patch.object(
                    inbox_consumer, "lookup_persisted_event"
                ) as lookup, mock.patch.object(
                    inbox_consumer, "persist_analysis"
                ) as persist:
                    outcome = self.make_consumer()._consume_event(
                        invalid_event,
                        None,
                    )

                self.assertEqual(outcome, ConsumerStatus.REJECTED)
                lookup.assert_not_called()
                analyze.assert_not_called()
                persist.assert_not_called()

    def test_inbox_record_missing_identity_is_rejected_before_ai(self):
        event_path = self.inbox_directory / inbox_event_filename(
            self.event.ingestion_key
        )
        serialized = json.loads(event_path.read_text(encoding="utf-8"))
        serialized.pop("ingestion_key")
        event_path.write_text(json.dumps(serialized), encoding="utf-8")

        with mock.patch.object(
            inbox_consumer, "analyze_event"
        ) as analyze, mock.patch.object(
            inbox_consumer, "lookup_persisted_event"
        ) as lookup, mock.patch.object(
            inbox_consumer, "persist_analysis"
        ) as persist:
            with self.assertRaises(ConsumerIntegrityError):
                self.make_consumer().consume_once()

        lookup.assert_not_called()
        analyze.assert_not_called()
        persist.assert_not_called()

    def test_event_without_sidecar_is_pending_after_restart(self):
        analyze_result = self.analysis_result(pipeline_main.AIStatus.AI_RETRY)
        with mock.patch.object(
            inbox_consumer, "analyze_event", return_value=analyze_result
        ) as analyze:
            summary = self.make_consumer().consume_once()

        self.assertEqual(summary.retried, 1)
        analyze.assert_called_once()
        self.assertEqual(self.read_state()["status"], ConsumerStatus.RETRY.value)

    def test_due_retry_is_reprocessed_after_restart(self):
        with mock.patch.object(
            inbox_consumer,
            "analyze_event",
            return_value=self.analysis_result(pipeline_main.AIStatus.AI_RETRY),
        ):
            self.make_consumer().consume_once()
        state = self.read_state()
        state["next_attempt_at"] = (
            self.now - timedelta(seconds=1)
        ).isoformat()
        self.status_path().write_text(json.dumps(state), encoding="utf-8")

        with mock.patch.object(
            inbox_consumer, "analyze_event", return_value=self.analysis_result()
        ) as analyze, mock.patch.object(
            inbox_consumer,
            "persist_analysis",
            return_value=self.successful_persistence(),
        ):
            summary = self.make_consumer().consume_once()

        self.assertEqual(summary.processed, 1)
        analyze.assert_called_once()
        self.assertEqual(self.read_state()["status"], ConsumerStatus.PROCESSED.value)

    def test_retry_not_yet_due_is_skipped(self):
        with mock.patch.object(
            inbox_consumer,
            "analyze_event",
            return_value=self.analysis_result(pipeline_main.AIStatus.AI_RETRY),
        ):
            self.make_consumer().consume_once()

        with mock.patch.object(inbox_consumer, "analyze_event") as analyze:
            summary = self.make_consumer().consume_once()

        self.assertEqual(summary.skipped, 1)
        analyze.assert_not_called()

    def test_rejected_event_is_not_sent_to_ai_again(self):
        with mock.patch.object(
            inbox_consumer,
            "analyze_event",
            return_value=self.analysis_result(
                pipeline_main.AIStatus.AI_INVALID_RESPONSE
            ),
        ) as analyze:
            first = self.make_consumer().consume_once()
        rejected_state = self.status_path().read_bytes()

        with mock.patch.object(inbox_consumer, "analyze_event") as analyze_again:
            second = self.make_consumer().consume_once()

        self.assertEqual(first.rejected, 1)
        self.assertEqual(second.skipped, 1)
        analyze.assert_called_once()
        analyze_again.assert_not_called()
        self.assertEqual(self.read_state()["status"], ConsumerStatus.REJECTED.value)
        self.assertEqual(self.status_path().read_bytes(), rejected_state)

    def test_quarantined_event_does_not_call_ai(self):
        self.inbox_directory.joinpath(
            inbox_event_filename(self.event.ingestion_key)
        ).unlink()
        quarantined = self.create_event(b"\xff\n")
        self.publish(quarantined)

        with mock.patch.object(inbox_consumer, "analyze_event") as analyze:
            summary = self.make_consumer().consume_once()

        self.assertEqual(summary.rejected, 1)
        analyze.assert_not_called()
        self.assertEqual(
            self.read_state(quarantined.ingestion_key)["status"],
            ConsumerStatus.REJECTED.value,
        )

    def test_parser_error_does_not_call_ai(self):
        self.inbox_directory.joinpath(
            inbox_event_filename(self.event.ingestion_key)
        ).unlink()
        invalid_schema = self.create_event(b'{"not_the_logger_schema":true}\n')
        self.publish(invalid_schema)

        with mock.patch.object(inbox_consumer, "analyze_event") as analyze:
            summary = self.make_consumer().consume_once()

        self.assertEqual(summary.rejected, 1)
        analyze.assert_not_called()
        self.assertEqual(
            self.read_state(invalid_schema.ingestion_key)["status"],
            ConsumerStatus.REJECTED.value,
        )

    def test_ai_retry_creates_retry_not_processed(self):
        with mock.patch.object(
            inbox_consumer,
            "analyze_event",
            return_value=self.analysis_result(pipeline_main.AIStatus.AI_RETRY),
        ):
            summary = self.make_consumer().consume_once()

        state = self.read_state()
        self.assertEqual(summary.retried, 1)
        self.assertEqual(state["status"], ConsumerStatus.RETRY.value)
        self.assertEqual(state["retry_count"], 1)
        self.assertIsNotNone(state["next_attempt_at"])

    def test_ai_invalid_response_is_rejected_not_processed(self):
        with mock.patch.object(
            inbox_consumer,
            "analyze_event",
            return_value=self.analysis_result(
                pipeline_main.AIStatus.AI_INVALID_RESPONSE
            ),
        ):
            summary = self.make_consumer().consume_once()

        self.assertEqual(summary.rejected, 1)
        self.assertEqual(self.read_state()["status"], ConsumerStatus.REJECTED.value)

    def test_db_failure_creates_retry_and_never_processed(self):
        with mock.patch.object(
            inbox_consumer, "analyze_event", return_value=self.analysis_result()
        ), mock.patch.object(
            inbox_consumer,
            "persist_analysis",
            side_effect=pipeline_main.DatabasePersistenceError(
                "db unavailable", OSError("db unavailable")
            ),
        ):
            summary = self.make_consumer().consume_once()

        self.assertEqual(summary.retried, 1)
        self.assertEqual(self.read_state()["status"], ConsumerStatus.RETRY.value)

    def test_retry_limit_transitions_to_rejected(self):
        consumer = self.make_consumer(max_retries=2)
        failure = self.analysis_result(pipeline_main.AIStatus.AI_RETRY)
        with mock.patch.object(
            inbox_consumer, "analyze_event", return_value=failure
        ):
            consumer.consume_once()
            state = self.read_state()
            state["next_attempt_at"] = (self.now - timedelta(seconds=1)).isoformat()
            self.status_path().write_text(json.dumps(state), encoding="utf-8")
            summary = consumer.consume_once()

        self.assertEqual(summary.rejected, 1)
        state = self.read_state()
        self.assertEqual(state["status"], ConsumerStatus.REJECTED.value)
        self.assertEqual(state["retry_count"], 2)

    def test_retry_sidecar_at_limit_fails_closed(self):
        state = {
            "version": CONSUMER_STATE_VERSION,
            "ingestion_key": self.event.ingestion_key,
            "status": ConsumerStatus.RETRY.value,
            "retry_count": 5,
            "updated_at": self.now.isoformat(),
            "reason": "retry limit reached",
            "next_attempt_at": self.now.isoformat(),
        }
        self.status_path().write_text(json.dumps(state), encoding="utf-8")

        with mock.patch.object(inbox_consumer, "analyze_event") as analyze:
            with self.assertRaises(ConsumerIntegrityError):
                self.make_consumer(max_retries=5).consume_once()

        analyze.assert_not_called()

    def test_corrupt_sidecar_fails_closed(self):
        self.status_path().write_text("{broken", encoding="utf-8")

        with self.assertRaises(ConsumerIntegrityError):
            self.make_consumer().consume_once()

    def test_sidecar_key_mismatch_fails_closed(self):
        self.status_path().write_text(
            json.dumps(
                {
                    "version": CONSUMER_STATE_VERSION,
                    "ingestion_key": "wrong-key",
                    "status": "PROCESSED",
                    "retry_count": 0,
                    "updated_at": self.now.isoformat(),
                    "reason": None,
                    "next_attempt_at": None,
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaises(ConsumerIntegrityError):
            self.make_consumer().consume_once()

    def test_corrupt_event_checksum_fails_closed(self):
        path = self.inbox_directory / inbox_event_filename(self.event.ingestion_key)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["raw_record_bytes_b64"] = base64.b64encode(b"tampered\n").decode("ascii")
        path.write_text(json.dumps(data), encoding="utf-8")

        with self.assertRaises(ConsumerIntegrityError):
            self.make_consumer().consume_once()

    def test_processed_state_cannot_be_overwritten(self):
        with mock.patch.object(
            inbox_consumer, "analyze_event", return_value=self.analysis_result()
        ), mock.patch.object(
            inbox_consumer,
            "persist_analysis",
            return_value=self.successful_persistence(),
        ):
            self.make_consumer().consume_once()

        with self.assertRaises(ConsumerStateError):
            self.make_consumer()._write_state(
                inbox_consumer.ConsumerState(
                    version=CONSUMER_STATE_VERSION,
                    ingestion_key=self.event.ingestion_key,
                    status=ConsumerStatus.RETRY,
                    retry_count=1,
                    updated_at=self.now.isoformat(),
                    reason="should not overwrite",
                    next_attempt_at=(self.now + timedelta(seconds=1)).isoformat(),
                )
            )

    def test_sidecar_directory_sync_failure_is_not_acknowledged(self):
        with mock.patch.object(
            inbox_consumer,
            "analyze_event",
            return_value=self.analysis_result(),
        ), mock.patch.object(
            inbox_consumer,
            "persist_analysis",
            return_value=self.successful_persistence(),
        ), mock.patch.object(
            inbox_consumer,
            "_fsync_directory",
            side_effect=NotImplementedError("unsupported"),
        ):
            with self.assertRaises(DurabilityError):
                self.make_consumer().consume_once()

        self.assertTrue(self.status_path().exists())
        self.assertEqual(
            json.loads(self.status_path().read_text(encoding="utf-8"))["status"],
            ConsumerStatus.PROCESSED.value,
        )

    def test_two_consumer_processes_cannot_hold_consumer_lock(self):
        consumer = self.make_consumer()
        with consumer._lock:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    _TRY_LOCK_SCRIPT,
                    str(PIPELINE_DIR),
                    str(self.lock_path),
                    str(self.checkpoint_path),
                    str(self.inbox_directory),
                ],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "CONTENDED")

    def test_crash_after_db_commit_before_processed_sidecar_uses_database_precheck(self):
        consumer = self.make_consumer()
        with mock.patch.object(
            inbox_consumer,
            "lookup_persisted_event",
            side_effect=[None, self.already_persisted_result()],
        ) as lookup, mock.patch.object(
            inbox_consumer,
            "analyze_event",
            return_value=self.analysis_result(),
        ) as analyze, mock.patch.object(
            inbox_consumer,
            "persist_analysis",
            return_value=self.successful_persistence(),
        ) as persist, mock.patch.object(
            consumer,
            "_write_state",
            side_effect=DurabilityError(
                "crash before processed state publication",
                operation="consumer_state_write",
                publication_state=PublicationState.NOT_PUBLISHED,
                directory_sync_state=DirectorySyncState.NOT_ATTEMPTED,
                destination=self.status_path(),
            ),
        ):
            with self.assertRaises(DurabilityError):
                consumer.consume_once()

            summary = self.make_consumer().consume_once()

        self.assertEqual(summary.processed, 1)
        self.assertEqual(lookup.call_count, 2)
        analyze.assert_called_once()
        persist.assert_called_once()

    def test_review_commit_before_sidecar_retry_uses_precheck_without_ai(self):
        consumer = self.make_consumer()
        review = SimpleNamespace(
            status=pipeline_main.AIStatus.AI_SUCCESS,
            classification_status="NEEDS_REVIEW",
            attack_type=None,
            confidence=None,
            risk_score=None,
            reason="INFERENCE_ERROR",
            source="Local_ML",
            recommendation=None,
        )
        with mock.patch.object(
            inbox_consumer,
            "lookup_persisted_event",
            side_effect=[None, self.already_persisted_result()],
        ) as lookup, mock.patch.object(
            inbox_consumer,
            "analyze_event",
            return_value=review,
        ) as analyze, mock.patch.object(
            inbox_consumer,
            "persist_analysis",
            return_value=self.successful_persistence(),
        ) as persist, mock.patch.object(
            consumer,
            "_write_state",
            side_effect=DurabilityError(
                "crash before processed state publication",
                operation="consumer_state_write",
                publication_state=PublicationState.NOT_PUBLISHED,
                directory_sync_state=DirectorySyncState.NOT_ATTEMPTED,
                destination=self.status_path(),
            ),
        ):
            with self.assertRaises(DurabilityError):
                consumer.consume_once()

            summary = self.make_consumer().consume_once()

        self.assertEqual(summary.processed, 1)
        self.assertEqual(lookup.call_count, 2)
        analyze.assert_called_once()
        persist.assert_called_once()


class DatabaseTransactionTests(unittest.TestCase):
    def setUp(self):
        self.raw_record_bytes = InboxConsumerTests.valid_record()
        self.record = self.raw_record_bytes.decode("utf-8")
        self.event = pipeline_main.parse_record(self.record)
        self.ingestion_key = create_ingestion_key(
            "database-test-generation",
            0,
            len(self.raw_record_bytes),
        )
        self.inbox_event = create_inbox_event(
            "database-test-generation",
            0,
            len(self.raw_record_bytes),
            self.raw_record_bytes,
        )
        self.source_record_sha256 = hashlib.sha256(
            self.raw_record_bytes
        ).hexdigest()
        self.analysis = pipeline_main.AIAnalysisResult(
            status=pipeline_main.AIStatus.AI_SUCCESS,
            attack_type="SQLi",
            confidence=0.9,
            risk_score=7,
            source="Local_ML",
            recommendation="Use parameterized SQL statements.",
        )

    def test_idempotency_metadata_is_canonical_and_uses_binary_sha256(self):
        metadata = pipeline_main._idempotency_metadata(
            self.ingestion_key,
            self.source_record_sha256,
        )
        validated_metadata = pipeline_main.validate_ingestion_metadata(
            self.event,
            self.inbox_event,
        )

        self.assertEqual(metadata.ingestion_key_bytes, self.ingestion_key.encode("utf-8"))
        self.assertEqual(
            metadata.ingestion_key_hash,
            hashlib.sha256(self.ingestion_key.encode("utf-8")).digest(),
        )
        self.assertEqual(
            metadata.source_record_sha256,
            hashlib.sha256(self.raw_record_bytes).digest(),
        )
        self.assertEqual(
            validated_metadata.source_record_sha256,
            hashlib.sha256(self.raw_record_bytes).digest(),
        )
        self.assertTrue(self.raw_record_bytes.endswith(b"\n"))

    def test_invalid_or_oversized_idempotency_metadata_fails_before_connect(self):
        with mock.patch.object(
            pipeline_main.mysql.connector, "connect"
        ) as connect:
            invalid_events = (
                replace(self.inbox_event, ingestion_key="not-json"),
                replace(
                    self.inbox_event,
                    source_file_identity="different-generation",
                ),
                replace(self.inbox_event, byte_offset_start=1),
                replace(self.inbox_event, raw_bytes_sha256="0" * 64),
            )
            for invalid_event in invalid_events:
                with self.subTest(invalid_event=invalid_event):
                    with self.assertRaises(
                        pipeline_main.IdempotencyIntegrityError
                    ):
                        pipeline_main.persist_analysis(
                            self.event,
                            self.analysis,
                            inbox_event=invalid_event,
                        )
            with self.assertRaises(pipeline_main.IdempotencyIntegrityError):
                pipeline_main.persist_analysis(
                    self.event,
                    self.analysis,
                    inbox_event=None,
                )
            with mock.patch.object(
                pipeline_main, "MAX_INGESTION_KEY_BYTES", 1
            ), self.assertRaises(pipeline_main.IdempotencyIntegrityError):
                pipeline_main.persist_analysis(
                    self.event,
                    self.analysis,
                    inbox_event=self.inbox_event,
                )

        connect.assert_not_called()

    def test_utf8_source_hash_uses_original_bytes_including_newline(self):
        raw_record_bytes = self.raw_record_bytes.replace(
            b'"content":"payload"',
            '"content":"café"'.encode("utf-8"),
        )
        self.assertTrue(raw_record_bytes.endswith(b"\n"))
        inbox_event = create_inbox_event(
            "utf8-source-generation",
            0,
            len(raw_record_bytes),
            raw_record_bytes,
        )
        event = pipeline_main.parse_record(raw_record_bytes.decode("utf-8"))

        metadata = pipeline_main.validate_ingestion_metadata(event, inbox_event)

        self.assertEqual(
            metadata.source_record_sha256,
            hashlib.sha256(raw_record_bytes).digest(),
        )
        self.assertNotEqual(
            metadata.source_record_sha256,
            hashlib.sha256(event.raw_record.rstrip("\n").encode("utf-8")).digest(),
        )

    def test_persistence_rejects_normalized_fields_changed_from_raw_record(self):
        for field, altered_value in (
            ("source_ip", "203.0.113.99"),
            ("method", "GET"),
            ("uri", "/different.php"),
        ):
            with self.subTest(field=field), mock.patch.object(
                pipeline_main,
                "lookup_persisted_event",
            ) as lookup, mock.patch.object(
                pipeline_main.mysql.connector, "connect"
            ) as connect:
                altered_event = replace(self.event, **{field: altered_value})
                with self.assertRaises(
                    pipeline_main.IdempotencyIntegrityError
                ):
                    pipeline_main.persist_analysis(
                        altered_event,
                        self.analysis,
                        inbox_event=self.inbox_event,
                    )

                lookup.assert_not_called()
                connect.assert_not_called()

    def test_direct_persistence_skips_insert_when_precheck_finds_existing_event(self):
        existing = pipeline_main.DatabasePersistenceResult(
            status=pipeline_main.DatabasePersistenceStatus.DB_SUCCESS,
            log_id=92,
            already_persisted=True,
            recommendation="Persisted recommendation.",
        )
        with mock.patch.object(
            pipeline_main,
            "lookup_persisted_event",
            return_value=existing,
        ) as lookup, mock.patch.object(
            pipeline_main.mysql.connector, "connect"
        ) as connect, mock.patch.object(
            pipeline_main, "send_email_alert"
        ) as email_alert:
            result = pipeline_main.persist_analysis(
                self.event,
                self.analysis,
                inbox_event=self.inbox_event,
            )

        self.assertIs(result, existing)
        self.assertEqual(result.recommendation, "Persisted recommendation.")
        lookup.assert_called_once_with(self.event, self.inbox_event)
        connect.assert_not_called()
        email_alert.assert_not_called()

    def test_persistence_lookup_failure_prevents_database_write(self):
        lookup_error = pipeline_main.DatabasePersistenceError(
            "database lookup unavailable",
            OSError("database unavailable"),
        )
        with mock.patch.object(
            pipeline_main,
            "lookup_persisted_event",
            side_effect=lookup_error,
        ) as lookup, mock.patch.object(
            pipeline_main.mysql.connector, "connect"
        ) as connect:
            with self.assertRaises(pipeline_main.DatabasePersistenceError):
                pipeline_main.persist_analysis(
                    self.event,
                    self.analysis,
                    inbox_event=self.inbox_event,
                )

        lookup.assert_called_once_with(self.event, self.inbox_event)
        connect.assert_not_called()

    def test_lookup_matching_row_verifies_related_records(self):
        connection = mock.Mock()
        cursor = mock.Mock()
        connection.cursor.return_value = cursor
        cursor.fetchone.side_effect = [
            (
                77,
                self.ingestion_key.encode("utf-8"),
                hashlib.sha256(self.raw_record_bytes).digest(),
            ),
            (0,),
        ]
        cursor.fetchall.return_value = [
            ("CLASSIFIED", None, "SQLi", 0.9, 7, "Local_ML", None)
        ]

        with mock.patch.object(
            pipeline_main.mysql.connector, "connect", return_value=connection
        ), mock.patch.dict(os.environ, {"MYSQL_PASSWORD": "test-only-password"}):
            result = pipeline_main.lookup_persisted_event(
                self.event,
                self.inbox_event,
            )

        self.assertIsNotNone(result)
        self.assertTrue(result.already_persisted)
        self.assertEqual(result.log_id, 77)
        self.assertIsNone(result.recommendation)
        self.assertEqual(cursor.execute.call_count, 3)
        connection.close.assert_called_once()

    def test_lookup_rejects_mismatched_canonical_key(self):
        connection = mock.Mock()
        cursor = mock.Mock()
        connection.cursor.return_value = cursor
        cursor.fetchone.return_value = (
            77,
            b"different canonical key",
            hashlib.sha256(self.raw_record_bytes).digest(),
        )

        with mock.patch.object(
            pipeline_main.mysql.connector, "connect", return_value=connection
        ), mock.patch.dict(os.environ, {"MYSQL_PASSWORD": "test-only-password"}):
            with self.assertRaises(pipeline_main.IdempotencyIntegrityError):
                pipeline_main.lookup_persisted_event(
                    self.event,
                    self.inbox_event,
                )

        cursor.fetchall.assert_not_called()
        connection.close.assert_called_once()

    def test_lookup_rejects_source_record_hash_mismatch(self):
        connection = mock.Mock()
        cursor = mock.Mock()
        connection.cursor.return_value = cursor
        cursor.fetchone.return_value = (
            77,
            self.ingestion_key.encode("utf-8"),
            bytes.fromhex("0" * 64),
        )

        with mock.patch.object(
            pipeline_main.mysql.connector, "connect", return_value=connection
        ), mock.patch.dict(os.environ, {"MYSQL_PASSWORD": "test-only-password"}):
            with self.assertRaises(pipeline_main.IdempotencyIntegrityError):
                pipeline_main.lookup_persisted_event(
                    self.event,
                    self.inbox_event,
                )

        cursor.fetchall.assert_not_called()

    def test_lookup_rejects_incomplete_related_rows(self):
        connection = mock.Mock()
        cursor = mock.Mock()
        connection.cursor.return_value = cursor
        cursor.fetchone.return_value = (
            77,
            self.ingestion_key.encode("utf-8"),
            hashlib.sha256(self.raw_record_bytes).digest(),
        )
        cursor.fetchall.return_value = []

        with mock.patch.object(
            pipeline_main.mysql.connector, "connect", return_value=connection
        ), mock.patch.dict(os.environ, {"MYSQL_PASSWORD": "test-only-password"}):
            with self.assertRaises(pipeline_main.IdempotencyIntegrityError):
                pipeline_main.lookup_persisted_event(
                    self.event,
                    self.inbox_event,
                )

    def test_persistence_stores_idempotency_metadata_in_raw_log(self):
        connection = mock.Mock()
        cursor = mock.Mock()
        cursor.lastrowid = 77
        connection.cursor.return_value = cursor

        with mock.patch.object(
            pipeline_main,
            "lookup_persisted_event",
            return_value=None,
        ), mock.patch.object(
            pipeline_main.mysql.connector, "connect", return_value=connection
        ), mock.patch.dict(
            os.environ, {"MYSQL_PASSWORD": "test-only-password"}
        ), mock.patch.object(
            pipeline_main, "send_email_alert"
        ), mock.patch("builtins.print"):
            result = pipeline_main.persist_analysis(
                self.event,
                self.analysis,
                inbox_event=self.inbox_event,
            )

        self.assertEqual(result.status, pipeline_main.DatabasePersistenceStatus.DB_SUCCESS)
        raw_insert_sql, raw_insert_parameters = cursor.execute.call_args_list[0].args
        self.assertIn("ingestion_key_hash", raw_insert_sql)
        self.assertEqual(
            raw_insert_parameters[5],
            hashlib.sha256(self.ingestion_key.encode("utf-8")).digest(),
        )
        self.assertEqual(raw_insert_parameters[6], self.ingestion_key.encode("utf-8"))
        self.assertEqual(
            raw_insert_parameters[7],
            hashlib.sha256(self.raw_record_bytes).digest(),
        )
        ai_insert_sql, ai_insert_parameters = cursor.execute.call_args_list[1].args
        self.assertIn("recommendation", ai_insert_sql)
        self.assertEqual(
            ai_insert_parameters[5],
            "Local_ML",
        )
        self.assertIsNone(ai_insert_parameters[6])
        self.assertEqual(
            ai_insert_parameters[7],
            "Use parameterized SQL statements.",
        )
        self.assertEqual(
            result.recommendation,
            "Use parameterized SQL statements.",
        )
        connection.commit.assert_called_once()

    def test_persistence_stores_null_recommendation(self):
        connection = mock.Mock()
        cursor = mock.Mock()
        cursor.lastrowid = 77
        connection.cursor.return_value = cursor
        analysis = replace(self.analysis, recommendation=None)

        with mock.patch.object(
            pipeline_main,
            "lookup_persisted_event",
            return_value=None,
        ), mock.patch.object(
            pipeline_main.mysql.connector, "connect", return_value=connection
        ), mock.patch.dict(
            os.environ, {"MYSQL_PASSWORD": "test-only-password"}
        ), mock.patch.object(
            pipeline_main, "send_email_alert"
        ), mock.patch("builtins.print"):
            result = pipeline_main.persist_analysis(
                self.event,
                analysis,
                inbox_event=self.inbox_event,
            )

        _, ai_insert_parameters = cursor.execute.call_args_list[1].args
        self.assertEqual(ai_insert_parameters[5], "Local_ML")
        self.assertIsNone(ai_insert_parameters[6])
        self.assertIsNone(ai_insert_parameters[7])
        self.assertIsNone(result.recommendation)
        connection.commit.assert_called_once()

    def test_ambiguous_commit_is_verified_on_fresh_connection(self):
        write_connection = mock.Mock()
        write_cursor = mock.Mock()
        write_cursor.lastrowid = 77
        write_connection.cursor.return_value = write_cursor
        write_connection.commit.side_effect = OSError("commit response lost")

        verify_connection = mock.Mock()
        verify_cursor = mock.Mock()
        verify_connection.cursor.return_value = verify_cursor
        verify_cursor.fetchone.side_effect = [
            (
                77,
                self.ingestion_key.encode("utf-8"),
                hashlib.sha256(self.raw_record_bytes).digest(),
            ),
            (0,),
        ]
        verify_cursor.fetchall.return_value = [
            (
                "CLASSIFIED",
                None,
                "SQLi",
                0.9,
                7,
                "Local_ML",
                "Stored recommendation.",
            )
        ]
        original_lookup = pipeline_main.lookup_persisted_event
        lookup_calls = 0

        def precheck_then_verify(event, inbox_event):
            nonlocal lookup_calls
            lookup_calls += 1
            if lookup_calls == 1:
                return None
            return original_lookup(event, inbox_event)

        with mock.patch.object(
            pipeline_main,
            "lookup_persisted_event",
            side_effect=precheck_then_verify,
        ), mock.patch.object(
            pipeline_main.mysql.connector,
            "connect",
            side_effect=[write_connection, verify_connection],
        ), mock.patch.dict(
            os.environ, {"MYSQL_PASSWORD": "test-only-password"}
        ), mock.patch.object(
            pipeline_main, "send_email_alert"
        ), mock.patch("builtins.print"):
            result = pipeline_main.persist_analysis(
                self.event,
                self.analysis,
                inbox_event=self.inbox_event,
            )

        self.assertTrue(result.already_persisted)
        self.assertEqual(result.log_id, 77)
        self.assertEqual(result.recommendation, "Stored recommendation.")
        self.assertEqual(lookup_calls, 2)
        write_connection.rollback.assert_called_once()
        verify_connection.close.assert_called_once()
        self.assertEqual(verify_cursor.execute.call_count, 3)

    def test_ambiguous_commit_without_visible_row_remains_retryable(self):
        write_connection = mock.Mock()
        write_cursor = mock.Mock()
        write_cursor.lastrowid = 77
        write_connection.cursor.return_value = write_cursor
        write_connection.commit.side_effect = OSError("commit outcome unknown")

        verify_connection = mock.Mock()
        verify_cursor = mock.Mock()
        verify_connection.cursor.return_value = verify_cursor
        verify_cursor.fetchone.return_value = None
        original_lookup = pipeline_main.lookup_persisted_event
        lookup_calls = 0

        def precheck_then_verify(event, inbox_event):
            nonlocal lookup_calls
            lookup_calls += 1
            if lookup_calls == 1:
                return None
            return original_lookup(event, inbox_event)

        with mock.patch.object(
            pipeline_main,
            "lookup_persisted_event",
            side_effect=precheck_then_verify,
        ), mock.patch.object(
            pipeline_main.mysql.connector,
            "connect",
            side_effect=[write_connection, verify_connection],
        ), mock.patch.dict(
            os.environ, {"MYSQL_PASSWORD": "test-only-password"}
        ), mock.patch.object(
            pipeline_main, "send_email_alert"
        ), mock.patch("builtins.print"):
            with self.assertRaises(pipeline_main.DatabasePersistenceError):
                pipeline_main.persist_analysis(
                    self.event,
                    self.analysis,
                    inbox_event=self.inbox_event,
                )

        write_connection.rollback.assert_called_once()
        self.assertEqual(lookup_calls, 2)
        verify_connection.close.assert_called_once()
        verify_cursor.execute.assert_called_once()

    def test_duplicate_key_conflict_verifies_existing_row_without_child_inserts(self):
        class DuplicateKeyError(Exception):
            errno = 1062

        write_connection = mock.Mock()
        write_cursor = mock.Mock()
        write_cursor.execute.side_effect = DuplicateKeyError("duplicate key")
        write_connection.cursor.return_value = write_cursor

        verify_connection = mock.Mock()
        verify_cursor = mock.Mock()
        verify_connection.cursor.return_value = verify_cursor
        verify_cursor.fetchone.side_effect = [
            (
                77,
                self.ingestion_key.encode("utf-8"),
                hashlib.sha256(self.raw_record_bytes).digest(),
            ),
            (0,),
        ]
        verify_cursor.fetchall.return_value = [
            (
                "CLASSIFIED",
                None,
                "SQLi",
                0.9,
                7,
                "Local_ML",
                "Stored recommendation.",
            )
        ]
        original_lookup = pipeline_main.lookup_persisted_event
        lookup_calls = 0

        def precheck_then_verify(event, inbox_event):
            nonlocal lookup_calls
            lookup_calls += 1
            if lookup_calls == 1:
                return None
            return original_lookup(event, inbox_event)

        with mock.patch.object(
            pipeline_main,
            "lookup_persisted_event",
            side_effect=precheck_then_verify,
        ), mock.patch.object(
            pipeline_main.mysql.connector,
            "connect",
            side_effect=[write_connection, verify_connection],
        ), mock.patch.dict(
            os.environ, {"MYSQL_PASSWORD": "test-only-password"}
        ), mock.patch.object(
            pipeline_main, "send_email_alert"
        ), mock.patch("builtins.print"):
            result = pipeline_main.persist_analysis(
                self.event,
                self.analysis,
                inbox_event=self.inbox_event,
            )

        self.assertTrue(result.already_persisted)
        self.assertEqual(result.recommendation, "Stored recommendation.")
        self.assertEqual(lookup_calls, 2)
        self.assertEqual(write_cursor.execute.call_count, 1)
        write_connection.rollback.assert_called_once()
        self.assertEqual(verify_cursor.execute.call_count, 3)

    def test_duplicate_key_conflict_with_mismatched_event_is_integrity_error(self):
        class DuplicateKeyError(Exception):
            errno = 1062

        write_connection = mock.Mock()
        write_cursor = mock.Mock()
        write_cursor.execute.side_effect = DuplicateKeyError("duplicate key")
        write_connection.cursor.return_value = write_cursor

        verify_connection = mock.Mock()
        verify_cursor = mock.Mock()
        verify_connection.cursor.return_value = verify_cursor
        verify_cursor.fetchone.return_value = (
            77,
            b"different canonical key",
            hashlib.sha256(self.raw_record_bytes).digest(),
        )
        original_lookup = pipeline_main.lookup_persisted_event
        lookup_calls = 0

        def precheck_then_verify(event, inbox_event):
            nonlocal lookup_calls
            lookup_calls += 1
            if lookup_calls == 1:
                return None
            return original_lookup(event, inbox_event)

        with mock.patch.object(
            pipeline_main,
            "lookup_persisted_event",
            side_effect=precheck_then_verify,
        ), mock.patch.object(
            pipeline_main.mysql.connector,
            "connect",
            side_effect=[write_connection, verify_connection],
        ), mock.patch.dict(
            os.environ, {"MYSQL_PASSWORD": "test-only-password"}
        ), mock.patch.object(
            pipeline_main, "send_email_alert"
        ), mock.patch("builtins.print"):
            with self.assertRaises(pipeline_main.IdempotencyIntegrityError):
                pipeline_main.persist_analysis(
                    self.event,
                    self.analysis,
                    inbox_event=self.inbox_event,
                )

        write_connection.rollback.assert_called_once()
        self.assertEqual(lookup_calls, 2)
        verify_cursor.fetchall.assert_not_called()

    def run_database_failure(self, failure_target):
        connection = mock.Mock()
        cursor = mock.Mock()
        cursor.lastrowid = 77
        connection.cursor.return_value = cursor
        if failure_target == "insert":
            cursor.execute.side_effect = [None, OSError("insert failed")]
        elif failure_target == "commit":
            connection.commit.side_effect = OSError("commit failed")
        with mock.patch.object(
            pipeline_main.mysql.connector, "connect", return_value=connection
        ), mock.patch.dict(
            os.environ, {"MYSQL_PASSWORD": "test-only-password"}
        ), mock.patch.object(
            pipeline_main,
            "lookup_persisted_event",
            return_value=None,
        ), mock.patch.object(
            pipeline_main, "send_email_alert"
        ), mock.patch.object(
            pipeline_main,
            "lookup_persisted_event",
            return_value=None,
        ), mock.patch("builtins.print"):
            with self.assertRaises(pipeline_main.DatabasePersistenceError):
                pipeline_main.persist_analysis(
                    self.event,
                    self.analysis,
                    inbox_event=self.inbox_event,
                )
        return connection, cursor

    def test_persistence_rejects_missing_or_unsupported_source_before_connect(self):
        for source in (None, "Unknown"):
                analysis = pipeline_main.AIAnalysisResult(
                    status=pipeline_main.AIStatus.AI_SUCCESS,
                    attack_type="SQLi",
                    confidence=0.9,
                    risk_score=7,
                    source=source,
                )
                with self.subTest(source=source), mock.patch.object(
                    pipeline_main.mysql.connector, "connect"
                ) as connect:
                    with self.assertRaisesRegex(ValueError, "valid source"):
                        pipeline_main.persist_analysis(
                            self.event,
                            analysis,
                            inbox_event=self.inbox_event,
                        )

                connect.assert_not_called()

    def test_insert_failure_rolls_back_and_closes_cursor_and_connection(self):
        connection, cursor = self.run_database_failure("insert")
        connection.rollback.assert_called_once()
        cursor.close.assert_called_once()
        connection.close.assert_called_once()
        connection.commit.assert_not_called()

    def test_commit_failure_rolls_back_and_closes_cursor_and_connection(self):
        connection, cursor = self.run_database_failure("commit")
        connection.rollback.assert_called_once()
        cursor.close.assert_called_once()
        connection.close.assert_called_once()
        connection.commit.assert_called_once()

    def test_db_success_is_reported_only_after_commit(self):
        connection = mock.Mock()
        cursor = mock.Mock()
        cursor.lastrowid = 77
        connection.cursor.return_value = cursor
        with mock.patch.object(
            pipeline_main,
            "lookup_persisted_event",
            return_value=None,
        ), mock.patch.object(
            pipeline_main.mysql.connector, "connect", return_value=connection
        ), mock.patch.dict(
            os.environ, {"MYSQL_PASSWORD": "test-only-password"}
        ), mock.patch.object(
            pipeline_main, "send_email_alert"
        ), mock.patch("builtins.print"):
            result = pipeline_main.persist_analysis(
                self.event,
                self.analysis,
                inbox_event=self.inbox_event,
            )

        self.assertEqual(
            result.status, pipeline_main.DatabasePersistenceStatus.DB_SUCCESS
        )
        self.assertEqual(
            cursor.execute.call_args_list[1].args[1],
            (
                77,
                "CLASSIFIED",
                "SQLi",
                0.9,
                7,
                "Local_ML",
                None,
                "Use parameterized SQL statements.",
            ),
        )
        connection.commit.assert_called_once()
        cursor.close.assert_called_once()
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
