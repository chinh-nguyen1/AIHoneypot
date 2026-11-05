import base64
import binascii
import hashlib
import json
import math
import os
import re
import threading
from dataclasses import dataclass
from enum import Enum

import requests
import mysql.connector
from decoder_adapter import payload_for_ai
from email_alert import send_email_alert
from config import LOG_FORMAT, AI_API_URL, REJECTED_FILE_PATH, get_mysql_config
from jsonl_parser import JSONLParser
from inbox_record import (
    InboxEvent,
    create_ingestion_key,
    inbox_event_from_json,
    inbox_event_to_json,
)
from normalized_event import NormalizedEvent
from parser_contract import LogParseError
from parser_dispatcher import ParserDispatcher, ParserDispatcherError

AI_FAILURE_SPOOL_LOCK = threading.Lock()
AI_SOURCES = frozenset({"Local_ML"})
AI_ATTACK_TYPES = frozenset({"Normal", "SQLi", "XSS"})
AI_CLASSIFICATION_STATUSES = frozenset({"CLASSIFIED", "NEEDS_REVIEW"})
AI_REVIEW_REASONS = frozenset(
    {"LOW_CONFIDENCE", "MODEL_UNAVAILABLE", "INFERENCE_ERROR"}
)
AI_CONFIDENCE_THRESHOLD = 0.85
MAX_INGESTION_KEY_BYTES = (1 << 24) - 1


class AIStatus(str, Enum):
    AI_SUCCESS = "AI_SUCCESS"
    AI_RETRY = "AI_RETRY"
    AI_INVALID_RESPONSE = "AI_INVALID_RESPONSE"


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


@dataclass(frozen=True)
class AIAnalysisResult:
    status: AIStatus
    attack_type: str | None = None
    confidence: float | None = None
    risk_score: int | None = None
    failure_reason: str | None = None
    source: str | None = None
    recommendation: str | None = None
    classification_status: str = "CLASSIFIED"
    reason: str | None = None


class DatabasePersistenceError(RuntimeError):
    """Raised when database persistence cannot be confirmed as complete."""

    def __init__(
        self,
        message: str,
        original_cause: BaseException,
        rollback_cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.original_cause = original_cause
        self.rollback_cause = rollback_cause


class IdempotencyIntegrityError(RuntimeError):
    """Raised when a stored idempotency key does not identify this source event."""


@dataclass(frozen=True)
class IdempotencyMetadata:
    ingestion_key_bytes: bytes
    ingestion_key_hash: bytes
    source_record_sha256: bytes


class DatabasePersistenceStatus(str, Enum):
    DB_SUCCESS = "DB_SUCCESS"


@dataclass(frozen=True)
class DatabasePersistenceResult:
    status: DatabasePersistenceStatus
    log_id: int
    already_persisted: bool = False
    recommendation: str | None = None


def _idempotency_metadata(
    ingestion_key: str,
    source_record_sha256: str,
) -> IdempotencyMetadata:
    if not isinstance(ingestion_key, str) or not ingestion_key:
        raise ValueError("ingestion_key must be a non-empty canonical key")
    try:
        key_bytes = ingestion_key.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("ingestion_key must be valid UTF-8") from exc
    if len(key_bytes) > MAX_INGESTION_KEY_BYTES:
        raise ValueError("ingestion_key exceeds the MEDIUMBLOB storage limit")

    try:
        components = json.loads(ingestion_key)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("ingestion_key is not valid canonical JSON") from exc
    if (
        not isinstance(components, list)
        or len(components) != 3
        or not isinstance(components[0], str)
        or not components[0]
        or type(components[1]) is not int
        or type(components[2]) is not int
        or components[1] < 0
        or components[2] <= components[1]
    ):
        raise ValueError("ingestion_key components are invalid")
    try:
        canonical_key = create_ingestion_key(*components)
    except (TypeError, ValueError) as exc:
        raise ValueError("ingestion_key components are invalid") from exc
    if canonical_key != ingestion_key:
        raise ValueError("ingestion_key is not in canonical serialized form")

    if (
        not isinstance(source_record_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", source_record_sha256) is None
    ):
        raise ValueError("source_record_sha256 must be a lowercase SHA-256 digest")

    return IdempotencyMetadata(
        ingestion_key_bytes=key_bytes,
        ingestion_key_hash=hashlib.sha256(key_bytes).digest(),
        source_record_sha256=bytes.fromhex(source_record_sha256),
    )


def validate_ingestion_metadata(
    event: NormalizedEvent,
    inbox_event: InboxEvent,
) -> IdempotencyMetadata:
    """Validate persisted identity against a structurally valid Inbox record."""
    if not isinstance(event, NormalizedEvent) or not isinstance(
        inbox_event, InboxEvent
    ):
        raise IdempotencyIntegrityError("event or Inbox metadata has an invalid type")

    try:
        validated_inbox_event = inbox_event_from_json(
            inbox_event_to_json(inbox_event)
        )
        raw_record_bytes = base64.b64decode(
            validated_inbox_event.raw_record_bytes_b64,
            validate=True,
        )
        decoded_raw_record = raw_record_bytes.decode("utf-8")
    except (
        binascii.Error,
        TypeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise IdempotencyIntegrityError(
            "Inbox metadata or original raw bytes failed validation"
        ) from exc

    if (
        decoded_raw_record != validated_inbox_event.raw_record
        or event.raw_record != decoded_raw_record
    ):
        raise IdempotencyIntegrityError(
            "parsed event does not match the original Inbox record bytes"
        )

    try:
        expected_event = parse_record(decoded_raw_record)
    except (LogParseError, ParserDispatcherError, TypeError, ValueError) as exc:
        raise IdempotencyIntegrityError(
            "original Inbox record cannot be parsed for persistence"
        ) from exc
    if event != expected_event:
        raise IdempotencyIntegrityError(
            "normalized event fields do not match the original Inbox record"
        )

    source_record_sha256 = hashlib.sha256(raw_record_bytes).hexdigest()
    if source_record_sha256 != validated_inbox_event.raw_bytes_sha256:
        raise IdempotencyIntegrityError(
            "Inbox source checksum does not match the original raw bytes"
        )

    try:
        return _idempotency_metadata(
            validated_inbox_event.ingestion_key,
            source_record_sha256,
        )
    except ValueError as exc:
        raise IdempotencyIntegrityError(
            "Inbox ingestion identity is invalid"
        ) from exc


def _is_mysql_duplicate_key_error(error: BaseException) -> bool:
    return getattr(error, "errno", None) == 1062


def _verify_persisted_event(
    cursor,
    metadata: IdempotencyMetadata,
) -> DatabasePersistenceResult | None:
    cursor.execute(
        "SELECT id, ingestion_key_bytes, source_record_sha256 "
        "FROM raw_logs WHERE ingestion_key_hash = %s",
        (metadata.ingestion_key_hash,),
    )
    row = cursor.fetchone()
    if row is None:
        return None

    log_id, stored_key, stored_source_hash = row
    try:
        stored_key = bytes(stored_key)
        stored_source_hash = bytes(stored_source_hash)
    except (TypeError, ValueError) as exc:
        raise IdempotencyIntegrityError(
            "stored idempotency metadata is invalid"
        ) from exc
    if (
        type(log_id) is not int
        or log_id < 1
        or stored_key != metadata.ingestion_key_bytes
        or stored_source_hash != metadata.source_record_sha256
    ):
        raise IdempotencyIntegrityError(
            "stored idempotency metadata conflicts with the Inbox event"
        )

    cursor.execute(
        "SELECT classification_status, classification_reason, attack_type, "
        "confidence_score, risk_score, ai_source, recommendation "
        "FROM ai_labels WHERE log_id = %s",
        (log_id,),
    )
    outcomes = cursor.fetchall()
    if len(outcomes) != 1 or len(outcomes[0]) != 7:
        raise IdempotencyIntegrityError(
            "persisted event does not have exactly one AI outcome"
        )
    (
        classification_status,
        classification_reason,
        attack_type,
        confidence,
        risk_score,
        source,
        recommendation,
    ) = outcomes[0]

    if (
        not isinstance(classification_status, str)
        or classification_status not in AI_CLASSIFICATION_STATUSES
        or source != "Local_ML"
        or (
            classification_reason is not None
            and (
                not isinstance(classification_reason, str)
                or classification_reason not in AI_REVIEW_REASONS
            )
        )
        or (
            attack_type is not None
            and (
                not isinstance(attack_type, str)
                or attack_type not in AI_ATTACK_TYPES
            )
        )
        or (
            confidence is not None
            and not _is_finite_number(confidence)
        )
        or (
            risk_score is not None
            and (
                type(risk_score) is not int
                or not 1 <= risk_score <= 10
            )
        )
        or (
            recommendation is not None
            and not isinstance(recommendation, str)
        )
    ):
        raise IdempotencyIntegrityError(
            "persisted AI outcome has invalid field types or values"
        )

    if classification_status == "CLASSIFIED":
        if (
            attack_type not in AI_ATTACK_TYPES
            or confidence is None
            or not AI_CONFIDENCE_THRESHOLD <= float(confidence) <= 1
            or risk_score is None
            or classification_reason is not None
        ):
            raise IdempotencyIntegrityError(
                "persisted CLASSIFIED outcome is inconsistent"
            )
        expected_pentest_count = 1 if risk_score >= 8 else 0
    else:
        if (
            classification_reason not in AI_REVIEW_REASONS
            or attack_type is not None
            or risk_score is not None
            or recommendation is not None
        ):
            raise IdempotencyIntegrityError(
                "persisted NEEDS_REVIEW outcome is inconsistent"
            )
        if classification_reason == "LOW_CONFIDENCE":
            if (
                confidence is None
                or not 0 <= float(confidence) < AI_CONFIDENCE_THRESHOLD
            ):
                raise IdempotencyIntegrityError(
                    "persisted LOW_CONFIDENCE outcome has invalid confidence"
                )
        elif confidence is not None:
            raise IdempotencyIntegrityError(
                "persisted review outcome must have null confidence"
            )
        expected_pentest_count = 0

    cursor.execute(
        "SELECT COUNT(*) FROM pentest_payloads WHERE log_id = %s", (log_id,)
    )
    payload_count_row = cursor.fetchone()
    if (
        payload_count_row is None
        or type(payload_count_row[0]) is not int
        or payload_count_row[0] != expected_pentest_count
    ):
        raise IdempotencyIntegrityError(
            "persisted event has an incomplete or unexpected pentest payload set"
        )

    return DatabasePersistenceResult(
        status=DatabasePersistenceStatus.DB_SUCCESS,
        log_id=log_id,
        already_persisted=True,
        recommendation=recommendation,
    )


def lookup_persisted_event(
    event: NormalizedEvent,
    inbox_event: InboxEvent,
) -> DatabasePersistenceResult | None:
    """Verify an event already stored under its canonical Inbox identity."""
    metadata = validate_ingestion_metadata(event, inbox_event)
    connection = None
    cursor = None
    result = None
    error = None
    try:
        connection = mysql.connector.connect(**get_mysql_config())
        cursor = connection.cursor()
        result = _verify_persisted_event(cursor, metadata)
    except IdempotencyIntegrityError as exc:
        error = exc
    except Exception as exc:
        error = exc
    finally:
        for resource in (cursor, connection):
            if resource is not None:
                try:
                    resource.close()
                except Exception as exc:
                    if error is None:
                        error = exc

    if isinstance(error, IdempotencyIntegrityError):
        raise error
    if error is not None:
        raise DatabasePersistenceError(
            "database idempotency lookup failed",
            error,
        ) from error
    return result


def _create_parser_dispatcher() -> ParserDispatcher:
    dispatcher = ParserDispatcher()
    dispatcher.register("jsonl", JSONLParser())
    return dispatcher


PARSER_DISPATCHER = _create_parser_dispatcher()


def parse_record(raw_record: str) -> NormalizedEvent:
    """Parse one record using the explicitly configured source format."""
    return PARSER_DISPATCHER.parse(LOG_FORMAT, raw_record)


def analyze_with_ai(payload: str) -> AIAnalysisResult:
    """Call the AI service and validate its classification response."""
    try:
        response = requests.post(AI_API_URL, json={"payload": payload}, timeout=2)
    except requests.RequestException as exc:
        return AIAnalysisResult(
            status=AIStatus.AI_RETRY,
            failure_reason=type(exc).__name__,
        )

    status_code = response.status_code
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        return AIAnalysisResult(
            status=AIStatus.AI_INVALID_RESPONSE,
            failure_reason="invalid_http_status",
        )
    if status_code != 200:
        status = (
            AIStatus.AI_RETRY
            if status_code in (408, 425, 429)
            or 500 <= status_code <= 599
            else AIStatus.AI_INVALID_RESPONSE
        )
        return AIAnalysisResult(
            status=status,
            failure_reason=f"http_status_{status_code}",
        )

    try:
        data = response.json()
    except requests.exceptions.JSONDecodeError:
        return AIAnalysisResult(
            status=AIStatus.AI_INVALID_RESPONSE,
            failure_reason="invalid_json",
        )
    except requests.RequestException as exc:
        return AIAnalysisResult(
            status=AIStatus.AI_RETRY,
            failure_reason=type(exc).__name__,
        )
    except ValueError:
        return AIAnalysisResult(
            status=AIStatus.AI_INVALID_RESPONSE,
            failure_reason="invalid_json",
        )

    if not isinstance(data, dict):
        return AIAnalysisResult(
            status=AIStatus.AI_INVALID_RESPONSE,
            failure_reason="response_not_object",
        )

    required_fields = {
        "source",
        "classification_status",
        "attack_type",
        "confidence",
        "risk_score",
        "reason",
        "recommendation",
    }
    if not required_fields.issubset(data):
        return AIAnalysisResult(
            status=AIStatus.AI_INVALID_RESPONSE,
            failure_reason="missing_contract_fields",
        )

    attack_type = data.get("attack_type")
    confidence = data.get("confidence")
    risk_score = data.get("risk_score")
    source = data.get("source")
    classification_status = data.get("classification_status")
    reason = data.get("reason")
    recommendation = data.get("recommendation")
    if not isinstance(source, str) or source not in AI_SOURCES:
        return AIAnalysisResult(
            status=AIStatus.AI_INVALID_RESPONSE,
            failure_reason="invalid_or_missing_source",
        )
    if (
        not isinstance(classification_status, str)
        or classification_status not in AI_CLASSIFICATION_STATUSES
    ):
        return AIAnalysisResult(
            status=AIStatus.AI_INVALID_RESPONSE,
            failure_reason="invalid_classification_status",
        )
    if recommendation is not None and not isinstance(recommendation, str):
        return AIAnalysisResult(
            status=AIStatus.AI_INVALID_RESPONSE,
            failure_reason="invalid_recommendation",
        )

    confidence_value = None
    if confidence is not None:
        if not _is_finite_number(confidence):
            return AIAnalysisResult(
                status=AIStatus.AI_INVALID_RESPONSE,
                failure_reason="invalid_confidence",
            )
        confidence_value = float(confidence)
        if not 0 <= confidence_value <= 1:
            return AIAnalysisResult(
                status=AIStatus.AI_INVALID_RESPONSE,
                failure_reason="invalid_confidence",
            )

    if classification_status == "CLASSIFIED":
        if (
            not isinstance(attack_type, str)
            or attack_type not in AI_ATTACK_TYPES
        ):
            return AIAnalysisResult(
                status=AIStatus.AI_INVALID_RESPONSE,
                failure_reason="unsupported_attack_type",
            )
        if confidence_value is None or confidence_value < AI_CONFIDENCE_THRESHOLD:
            return AIAnalysisResult(
                status=AIStatus.AI_INVALID_RESPONSE,
                failure_reason="invalid_classified_confidence",
            )
        if type(risk_score) is not int or not 1 <= risk_score <= 10:
            return AIAnalysisResult(
                status=AIStatus.AI_INVALID_RESPONSE,
                failure_reason="invalid_risk_score",
            )
        if reason is not None:
            return AIAnalysisResult(
                status=AIStatus.AI_INVALID_RESPONSE,
                failure_reason="invalid_classified_reason",
            )
    else:
        if (
            attack_type is not None
            or risk_score is not None
            or recommendation is not None
            or not isinstance(reason, str)
            or reason not in AI_REVIEW_REASONS
        ):
            return AIAnalysisResult(
                status=AIStatus.AI_INVALID_RESPONSE,
                failure_reason="invalid_review_fields",
            )
        if reason == "LOW_CONFIDENCE":
            if (
                confidence_value is None
                or confidence_value >= AI_CONFIDENCE_THRESHOLD
            ):
                return AIAnalysisResult(
                    status=AIStatus.AI_INVALID_RESPONSE,
                    failure_reason="invalid_review_confidence",
                )
        elif confidence_value is not None:
            return AIAnalysisResult(
                status=AIStatus.AI_INVALID_RESPONSE,
                failure_reason="invalid_review_confidence",
            )

    return AIAnalysisResult(
        status=AIStatus.AI_SUCCESS,
        attack_type=attack_type,
        confidence=confidence_value,
        risk_score=risk_score,
        source=source,
        recommendation=recommendation,
        classification_status=classification_status,
        reason=reason,
    )


def _persist_ai_failure(event: NormalizedEvent, result: AIAnalysisResult) -> None:
    record = {
        "status": result.status.value,
        "reason": result.failure_reason,
        "source_event_id": event.source_event_id,
        "raw_record": event.raw_record,
    }
    serialized = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    parent_directory = os.path.dirname(REJECTED_FILE_PATH)
    if parent_directory:
        os.makedirs(parent_directory, exist_ok=True)

    with AI_FAILURE_SPOOL_LOCK:
        with open(REJECTED_FILE_PATH, "a", encoding="utf-8") as spool:
            spool.write(serialized + "\n")
            spool.flush()
            os.fsync(spool.fileno())


def persist_analysis(
    event: NormalizedEvent,
    analysis: AIAnalysisResult,
    *,
    inbox_event: InboxEvent,
) -> DatabasePersistenceResult:
    """Persist an AI success backed by a validated Inbox source record."""
    if analysis.status is not AIStatus.AI_SUCCESS:
        raise ValueError("only successful AI analyses can be persisted")
    if (
        not isinstance(analysis.classification_status, str)
        or analysis.classification_status not in AI_CLASSIFICATION_STATUSES
    ):
        raise ValueError("successful AI analysis has an invalid classification status")
    if not isinstance(analysis.source, str) or analysis.source not in AI_SOURCES:
        raise ValueError("successful AI analysis is missing a valid source")
    if (
        analysis.recommendation is not None
        and not isinstance(analysis.recommendation, str)
    ):
        raise ValueError("successful AI analysis has an invalid recommendation")

    confidence = analysis.confidence
    if confidence is not None:
        if not _is_finite_number(confidence):
            raise ValueError("successful AI analysis has invalid confidence")
        confidence = float(confidence)
        if not 0 <= confidence <= 1:
            raise ValueError("successful AI analysis has invalid confidence")

    if analysis.classification_status == "CLASSIFIED":
        if (
            not isinstance(analysis.attack_type, str)
            or analysis.attack_type not in AI_ATTACK_TYPES
            or confidence is None
            or confidence < AI_CONFIDENCE_THRESHOLD
            or type(analysis.risk_score) is not int
            or not 1 <= analysis.risk_score <= 10
            or analysis.reason is not None
        ):
            raise ValueError("successful CLASSIFIED analysis is inconsistent")
        attack_type = analysis.attack_type
        risk_score = analysis.risk_score
    else:
        if (
            analysis.attack_type is not None
            or analysis.risk_score is not None
            or analysis.recommendation is not None
            or not isinstance(analysis.reason, str)
            or analysis.reason not in AI_REVIEW_REASONS
        ):
            raise ValueError("successful NEEDS_REVIEW analysis is inconsistent")
        if analysis.reason == "LOW_CONFIDENCE":
            if confidence is None or confidence >= AI_CONFIDENCE_THRESHOLD:
                raise ValueError("LOW_CONFIDENCE analysis has invalid confidence")
        elif confidence is not None:
            raise ValueError("review analysis must have null confidence")
        attack_type = None
        risk_score = None

    metadata = validate_ingestion_metadata(event, inbox_event)
    existing = lookup_persisted_event(event, inbox_event)
    if existing is not None:
        return existing

    client_ip = event.source_ip
    method = event.method
    uri = event.uri
    raw_payload = payload_for_ai(event)
    decoded = raw_payload

    if analysis.classification_status == "CLASSIFIED":
        print(f"[+] [{attack_type}] Bắt log từ IP {client_ip} | Risk: {risk_score}")

    # Alerting is intentionally not transactional; a retry may send it again.
    if analysis.classification_status == "CLASSIFIED" and risk_score >= 8:
        send_email_alert(client_ip, attack_type, risk_score, decoded)

    connection = None
    cursor = None
    log_id = None
    persistence_error = None
    rollback_error = None
    cleanup_error = None
    raw_insert_in_progress = False
    duplicate_key_conflict = False
    commit_attempted = False
    try:
        connection = mysql.connector.connect(**get_mysql_config())
        cursor = connection.cursor()

        sql_log = (
            "INSERT INTO raw_logs "
            "(client_ip, http_method, request_uri, raw_payload, decoded_payload, "
            "ingestion_key_hash, ingestion_key_bytes, source_record_sha256) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
        )
        sql_parameters = (
            client_ip,
            method,
            uri,
            raw_payload,
            decoded,
            metadata.ingestion_key_hash,
            metadata.ingestion_key_bytes,
            metadata.source_record_sha256,
        )
        raw_insert_in_progress = True
        cursor.execute(sql_log, sql_parameters)
        raw_insert_in_progress = False
        log_id = cursor.lastrowid
        if type(log_id) is not int or log_id < 1:
            raise RuntimeError("database did not return a valid raw_logs id")

        sql_ai = (
            "INSERT INTO ai_labels "
            "(log_id, classification_status, attack_type, confidence_score, "
            "risk_score, ai_source, classification_reason, recommendation) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
        )
        cursor.execute(
            sql_ai,
            (
                log_id,
                analysis.classification_status,
                attack_type,
                confidence,
                risk_score,
                analysis.source,
                analysis.reason,
                analysis.recommendation,
            ),
        )

        if analysis.classification_status == "CLASSIFIED" and risk_score >= 8:
            sql_pentest = "INSERT INTO pentest_payloads (log_id, attack_type, high_risk_payload) VALUES (%s, %s, %s)"
            cursor.execute(sql_pentest, (log_id, attack_type, decoded))

        commit_attempted = True
        connection.commit()
    except Exception as exc:
        persistence_error = exc
        duplicate_key_conflict = (
            raw_insert_in_progress
            and _is_mysql_duplicate_key_error(exc)
        )
        if connection is not None:
            try:
                connection.rollback()
            except Exception as rollback_exc:
                rollback_error = rollback_exc
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception as close_exc:
                cleanup_error = close_exc
        if connection is not None:
            try:
                connection.close()
            except Exception as close_exc:
                if cleanup_error is None:
                    cleanup_error = close_exc

    if persistence_error is not None:
        if duplicate_key_conflict or commit_attempted:
            try:
                existing = lookup_persisted_event(
                    event,
                    inbox_event,
                )
            except IdempotencyIntegrityError:
                raise
            except DatabasePersistenceError as verification_error:
                raise DatabasePersistenceError(
                    "database persistence outcome could not be verified",
                    persistence_error,
                    verification_error,
                ) from persistence_error
            if existing is not None:
                return existing
            if duplicate_key_conflict:
                raise IdempotencyIntegrityError(
                    "duplicate-key conflict did not resolve to the expected event"
                ) from persistence_error

        raise DatabasePersistenceError(
            "database transaction failed before its outcome was confirmed",
            persistence_error,
            rollback_error,
        ) from persistence_error
    if cleanup_error is not None and log_id is None:
        raise DatabasePersistenceError(
            "database resources could not be closed",
            cleanup_error,
        ) from cleanup_error
    if cleanup_error is not None:
        print(f"[-] Lỗi đóng tài nguyên CSDL sau commit: {cleanup_error}")

    return DatabasePersistenceResult(
        status=DatabasePersistenceStatus.DB_SUCCESS,
        log_id=log_id,
        recommendation=analysis.recommendation,
    )


def analyze_event(event: NormalizedEvent) -> AIAnalysisResult:
    """Analyze a normalized event without claiming database persistence."""
    payload = payload_for_ai(event)
    return analyze_with_ai(payload)


def process_line(line: str) -> AIAnalysisResult:
    """Parse and analyze without persistence; AI failures retain the legacy spool."""
    event = parse_record(line)
    analysis = analyze_event(event)
    if analysis.status is not AIStatus.AI_SUCCESS:
        _persist_ai_failure(event, analysis)
        print(
            f"[-] AI classification unavailable for event "
            f"{event.source_event_id}: {analysis.status.value}"
        )
    return analysis

if __name__ == "__main__":
    from log_watcher import start_watching

    start_watching()
