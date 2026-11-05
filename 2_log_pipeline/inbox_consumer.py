"""Durable consumer for events published by the log watcher."""

import base64
import errno
import hashlib
import json
import logging
import os
import stat
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional, Union

from config import (
    CHECKPOINT_FILE_PATH,
    CONSUMER_LOCK_FILE_PATH,
    CONSUMER_MAX_RETRIES,
    CONSUMER_RETRY_BASE_SECONDS,
    CONSUMER_RETRY_MAX_SECONDS,
    CONSUMER_STATE_DIRECTORY_PATH,
    INBOX_DIRECTORY_PATH,
    POLLING_INTERVAL,
)
from durability_contract import (
    DirectorySyncState,
    DurabilityError,
    PublicationState,
)
from inbox_record import (
    InboxEvent,
    InboxRecordError,
    ParseState,
    inbox_event_filename,
    inbox_event_from_json,
)
from parser_contract import LogParseError
from parser_dispatcher import ParserDispatcherError
from pipeline_main import (
    AIStatus,
    DatabasePersistenceError,
    DatabasePersistenceStatus,
    IdempotencyIntegrityError,
    analyze_event,
    lookup_persisted_event,
    parse_record,
    persist_analysis,
    validate_ingestion_metadata,
)
from single_writer_lock import SingleWriterLock


LOGGER = logging.getLogger(__name__)
PathLike = Union[str, os.PathLike]
CONSUMER_STATE_VERSION = 1
_CONSUMER_STATE_FIELDS = {
    "version",
    "ingestion_key",
    "status",
    "retry_count",
    "updated_at",
    "reason",
    "next_attempt_at",
}


class ConsumerError(RuntimeError):
    """Base error for Inbox consumer failures."""


class ConsumerIntegrityError(ConsumerError):
    """Raised when an Inbox event or its sidecar cannot be trusted."""


class ConsumerStateError(ConsumerError):
    """Raised when durable consumer state cannot be safely persisted."""


class ConsumerStatus(str, Enum):
    RETRY = "RETRY"
    PROCESSED = "PROCESSED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class ConsumerState:
    version: int
    ingestion_key: str
    status: ConsumerStatus
    retry_count: int
    updated_at: str
    reason: Optional[str]
    next_attempt_at: Optional[str]


@dataclass(frozen=True)
class ConsumeSummary:
    processed: int = 0
    retried: int = 0
    rejected: int = 0
    skipped: int = 0


def _parse_aware_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ConsumerIntegrityError(f"consumer state {field_name} is invalid")
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ConsumerIntegrityError(
            f"consumer state {field_name} is invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ConsumerIntegrityError(
            f"consumer state {field_name} must include a timezone"
        )
    return parsed


def _state_filename(ingestion_key: str) -> str:
    digest = hashlib.sha256(ingestion_key.encode("utf-8")).hexdigest()
    return f"{digest}.json"


def _reject_duplicate_state_fields(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ConsumerIntegrityError(
                f"consumer sidecar contains duplicate field: {key}"
            )
        data[key] = value
    return data


def _state_from_json(serialized: str, expected_key: str) -> ConsumerState:
    try:
        data = json.loads(
            serialized,
            object_pairs_hook=_reject_duplicate_state_fields,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ConsumerIntegrityError("consumer sidecar is invalid JSON") from exc
    if not isinstance(data, dict) or set(data) != _CONSUMER_STATE_FIELDS:
        raise ConsumerIntegrityError("consumer sidecar fields are invalid")
    if type(data["version"]) is not int or data["version"] != CONSUMER_STATE_VERSION:
        raise ConsumerIntegrityError("consumer sidecar version is unsupported")
    if data["ingestion_key"] != expected_key:
        raise ConsumerIntegrityError("consumer sidecar ingestion_key mismatch")
    try:
        status = ConsumerStatus(data["status"])
    except (TypeError, ValueError) as exc:
        raise ConsumerIntegrityError("consumer sidecar status is invalid") from exc
    retry_count = data["retry_count"]
    if type(retry_count) is not int or retry_count < 0:
        raise ConsumerIntegrityError("consumer sidecar retry_count is invalid")
    _parse_aware_timestamp(data["updated_at"], "updated_at")
    next_attempt_at = data["next_attempt_at"]
    if next_attempt_at is not None:
        _parse_aware_timestamp(next_attempt_at, "next_attempt_at")
    reason = data["reason"]
    if reason is not None and not isinstance(reason, str):
        raise ConsumerIntegrityError("consumer sidecar reason is invalid")
    if status is ConsumerStatus.RETRY and next_attempt_at is None:
        raise ConsumerIntegrityError("RETRY sidecar requires next_attempt_at")
    if status is not ConsumerStatus.RETRY and next_attempt_at is not None:
        raise ConsumerIntegrityError(
            "only RETRY sidecars may specify next_attempt_at"
        )
    if status in (ConsumerStatus.RETRY, ConsumerStatus.REJECTED) and not reason:
        raise ConsumerIntegrityError(
            f"{status.value} sidecar requires a reason"
        )
    if status is ConsumerStatus.PROCESSED and reason is not None:
        raise ConsumerIntegrityError("PROCESSED sidecar cannot contain a reason")
    return ConsumerState(
        version=data["version"],
        ingestion_key=expected_key,
        status=status,
        retry_count=retry_count,
        updated_at=data["updated_at"],
        reason=reason,
        next_attempt_at=next_attempt_at,
    )


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        raise NotImplementedError("directory fsync is unsupported on Windows")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_state_file(path: Path, state: ConsumerState) -> None:
    serialized = json.dumps(
        {
            "version": state.version,
            "ingestion_key": state.ingestion_key,
            "status": state.status.value,
            "retry_count": state.retry_count,
            "updated_at": state.updated_at,
            "reason": state.reason,
            "next_attempt_at": state.next_attempt_at,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    descriptor_open = True
    published = False
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as state_file:
            written = state_file.write(serialized)
            if written != len(serialized):
                raise OSError("incomplete consumer sidecar write")
            state_file.flush()
            os.fsync(state_file.fileno())
        descriptor_open = False
        os.close(descriptor)
        os.replace(temporary_path, path)
        published = True
        try:
            _fsync_directory(path.parent)
        except (NotImplementedError, OSError) as exc:
            sync_state = (
                DirectorySyncState.UNSUPPORTED
                if isinstance(exc, NotImplementedError)
                or getattr(exc, "errno", None)
                in {
                    errno.EINVAL,
                    getattr(errno, "ENOTSUP", errno.EINVAL),
                    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
                }
                else DirectorySyncState.FAILED
            )
            raise DurabilityError(
                "consumer sidecar was published but directory durability "
                "was not confirmed",
                operation="consumer_state_write",
                publication_state=PublicationState.PUBLISHED_UNCONFIRMED,
                directory_sync_state=sync_state,
                destination=path,
                original_cause=exc,
            ) from exc
    except DurabilityError:
        raise
    except OSError as exc:
        raise DurabilityError(
            "consumer sidecar could not be durably published",
            operation="consumer_state_write",
            publication_state=(
                PublicationState.PUBLISHED_UNCONFIRMED
                if published
                else PublicationState.NOT_PUBLISHED
            ),
            directory_sync_state=(
                DirectorySyncState.FAILED
                if published
                else DirectorySyncState.NOT_ATTEMPTED
            ),
            destination=path,
            original_cause=exc,
        ) from exc
    finally:
        if descriptor_open:
            os.close(descriptor)
        if temporary_path.exists():
            temporary_path.unlink()


class InboxConsumer:
    """Consume durable Inbox events using a separate single-writer lock."""

    def __init__(
        self,
        inbox_directory: PathLike = INBOX_DIRECTORY_PATH,
        state_directory: PathLike = CONSUMER_STATE_DIRECTORY_PATH,
        lock_path: PathLike = CONSUMER_LOCK_FILE_PATH,
        *,
        checkpoint_path: PathLike = CHECKPOINT_FILE_PATH,
        max_retries: int = CONSUMER_MAX_RETRIES,
        retry_base_seconds: float = CONSUMER_RETRY_BASE_SECONDS,
        retry_max_seconds: float = CONSUMER_RETRY_MAX_SECONDS,
        clock=None,
        sleeper=time.sleep,
    ) -> None:
        if type(max_retries) is not int or max_retries < 1:
            raise ValueError("max_retries must be a positive integer")
        if retry_base_seconds <= 0 or retry_max_seconds < retry_base_seconds:
            raise ValueError("retry backoff configuration is invalid")
        self.inbox_directory = Path(inbox_directory)
        self.state_directory = Path(state_directory)
        self.lock_path = Path(lock_path)
        normalized_inbox = self.inbox_directory.resolve(strict=False)
        normalized_state = self.state_directory.resolve(strict=False)
        if (
            normalized_state == normalized_inbox
            or normalized_inbox in normalized_state.parents
        ):
            raise ValueError("consumer state directory must be outside the Inbox")
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleeper = sleeper
        self._lock = SingleWriterLock(
            self.lock_path,
            checkpoint_path=checkpoint_path,
            inbox_directory=self.inbox_directory,
        )

    def consume_once(self) -> ConsumeSummary:
        """Scan and process eligible events while holding the consumer lock."""
        if not self.inbox_directory.is_dir():
            raise ConsumerError("Inbox directory must already exist")
        if not self.lock_path.parent.is_dir():
            raise ConsumerError("consumer lock parent directory must already exist")
        self.state_directory.mkdir(parents=True, exist_ok=True)

        with self._lock:
            return self._consume_locked()

    def run_forever(self, polling_interval: float = POLLING_INTERVAL) -> None:
        """Consume repeatedly while retaining exclusive consumer ownership."""
        if polling_interval <= 0:
            raise ValueError("polling_interval must be positive")
        if not self.inbox_directory.is_dir():
            raise ConsumerError("Inbox directory must already exist")
        if not self.lock_path.parent.is_dir():
            raise ConsumerError("consumer lock parent directory must already exist")
        self.state_directory.mkdir(parents=True, exist_ok=True)

        with self._lock:
            while True:
                self._consume_locked()
                self.sleeper(polling_interval)

    def _consume_locked(self) -> ConsumeSummary:
        events = self._load_events()
        processed = retried = rejected = skipped = 0
        for event in events:
            state = self._load_state(event.ingestion_key)
            if state is not None:
                if state.status in (ConsumerStatus.PROCESSED, ConsumerStatus.REJECTED):
                    skipped += 1
                    continue
                if state.status is ConsumerStatus.RETRY:
                    retry_time = _parse_aware_timestamp(
                        state.next_attempt_at, "next_attempt_at"
                    )
                    if retry_time > self.clock():
                        skipped += 1
                        continue

            outcome = self._consume_event(event, state)
            if outcome is ConsumerStatus.PROCESSED:
                processed += 1
            elif outcome is ConsumerStatus.RETRY:
                retried += 1
            else:
                rejected += 1
        return ConsumeSummary(processed, retried, rejected, skipped)

    def _load_events(self) -> list[InboxEvent]:
        events = []
        for path in self.inbox_directory.glob("*.event.json"):
            try:
                file_stat = path.lstat()
            except OSError as exc:
                raise ConsumerIntegrityError(
                    f"could not inspect Inbox event: {path.name}"
                ) from exc
            if not stat.S_ISREG(file_stat.st_mode):
                raise ConsumerIntegrityError(
                    f"Inbox event is not a regular file: {path.name}"
                )
            try:
                serialized = path.read_bytes().decode("utf-8")
                event = inbox_event_from_json(serialized)
            except (OSError, UnicodeError, InboxRecordError) as exc:
                raise ConsumerIntegrityError(
                    f"Inbox event failed validation: {path.name}"
                ) from exc
            if path.name != inbox_event_filename(event.ingestion_key):
                raise ConsumerIntegrityError(
                    f"Inbox filename does not match ingestion_key: {path.name}"
                )
            events.append(event)
        return sorted(
            events,
            key=lambda event: (
                event.source_file_identity,
                event.byte_offset_start,
                event.byte_offset_end,
                event.ingestion_key,
            ),
        )

    def _load_state(self, ingestion_key: str) -> Optional[ConsumerState]:
        path = self._state_path(ingestion_key)
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ConsumerIntegrityError(
                f"consumer sidecar could not be read: {path.name}"
            ) from exc
        try:
            serialized = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConsumerIntegrityError(
                f"consumer sidecar is not UTF-8: {path.name}"
            ) from exc
        state = _state_from_json(serialized, ingestion_key)
        if (
            state.status is ConsumerStatus.RETRY
            and state.retry_count >= self.max_retries
        ):
            raise ConsumerIntegrityError(
                "RETRY sidecar has exhausted the configured retry limit"
            )
        return state

    def _consume_event(
        self,
        inbox_event: InboxEvent,
        previous_state: Optional[ConsumerState],
    ) -> ConsumerStatus:
        retry_count = previous_state.retry_count if previous_state else 0
        if inbox_event.parse_state is ParseState.QUARANTINED:
            return self._reject(
                inbox_event.ingestion_key,
                retry_count,
                inbox_event.parse_error or "raw record is quarantined",
            )

        if inbox_event.raw_record is None:
            return self._reject(
                inbox_event.ingestion_key,
                retry_count,
                "raw record is unavailable for parsing",
            )
        try:
            event = parse_record(inbox_event.raw_record)
        except (LogParseError, ParserDispatcherError, ValueError) as exc:
            return self._reject(
                inbox_event.ingestion_key,
                retry_count,
                f"parser rejected event: {type(exc).__name__}: {exc}",
            )

        try:
            validate_ingestion_metadata(event, inbox_event)
        except IdempotencyIntegrityError as exc:
            return self._reject(
                inbox_event.ingestion_key,
                retry_count,
                f"invalid Inbox ingestion metadata: {exc}",
            )

        try:
            existing = lookup_persisted_event(event, inbox_event)
        except IdempotencyIntegrityError as exc:
            return self._reject(
                inbox_event.ingestion_key,
                retry_count,
                f"idempotency integrity conflict: {exc}",
            )
        except DatabasePersistenceError as exc:
            return self._record_retry(
                inbox_event.ingestion_key,
                retry_count,
                f"database idempotency lookup failed: {exc}",
            )
        if existing is not None:
            self._mark_processed(inbox_event.ingestion_key, retry_count)
            return ConsumerStatus.PROCESSED

        analysis = analyze_event(event)
        if analysis.status is AIStatus.AI_INVALID_RESPONSE:
            return self._reject(
                inbox_event.ingestion_key,
                retry_count,
                analysis.failure_reason or "AI response is invalid",
            )
        if analysis.status is AIStatus.AI_RETRY:
            return self._record_retry(
                inbox_event.ingestion_key,
                retry_count,
                analysis.failure_reason or "AI service requested retry",
            )

        try:
            persistence = persist_analysis(
                event,
                analysis,
                inbox_event=inbox_event,
            )
        except IdempotencyIntegrityError as exc:
            return self._reject(
                inbox_event.ingestion_key,
                retry_count,
                f"idempotency integrity conflict: {exc}",
            )
        except DatabasePersistenceError as exc:
            return self._record_retry(
                inbox_event.ingestion_key,
                retry_count,
                f"database persistence failed: {exc}",
            )
        if persistence.status is not DatabasePersistenceStatus.DB_SUCCESS:
            raise ConsumerError(
                "database persistence returned without DB_SUCCESS"
            )

        self._mark_processed(inbox_event.ingestion_key, retry_count)
        return ConsumerStatus.PROCESSED

    def _mark_processed(self, ingestion_key: str, retry_count: int) -> None:
        self._write_state(
            ConsumerState(
                version=CONSUMER_STATE_VERSION,
                ingestion_key=ingestion_key,
                status=ConsumerStatus.PROCESSED,
                retry_count=retry_count,
                updated_at=self._now().isoformat(),
                reason=None,
                next_attempt_at=None,
            )
        )

    def _record_retry(
        self, ingestion_key: str, retry_count: int, reason: str
    ) -> ConsumerStatus:
        next_retry_count = retry_count + 1
        if next_retry_count >= self.max_retries:
            return self._reject(ingestion_key, next_retry_count, reason)

        delay = min(
            self.retry_base_seconds * (2 ** (next_retry_count - 1)),
            self.retry_max_seconds,
        )
        self._write_state(
            ConsumerState(
                version=CONSUMER_STATE_VERSION,
                ingestion_key=ingestion_key,
                status=ConsumerStatus.RETRY,
                retry_count=next_retry_count,
                updated_at=self._now().isoformat(),
                reason=reason,
                next_attempt_at=(self._now() + timedelta(seconds=delay)).isoformat(),
            )
        )
        return ConsumerStatus.RETRY

    def _reject(
        self, ingestion_key: str, retry_count: int, reason: str
    ) -> ConsumerStatus:
        self._write_state(
            ConsumerState(
                version=CONSUMER_STATE_VERSION,
                ingestion_key=ingestion_key,
                status=ConsumerStatus.REJECTED,
                retry_count=retry_count,
                updated_at=self._now().isoformat(),
                reason=reason,
                next_attempt_at=None,
            )
        )
        return ConsumerStatus.REJECTED

    def _write_state(self, state: ConsumerState) -> None:
        current = self._load_state(state.ingestion_key)
        if current is not None and current.status is ConsumerStatus.PROCESSED:
            if state.status is not ConsumerStatus.PROCESSED:
                raise ConsumerStateError(
                    "PROCESSED consumer state cannot be overwritten"
                )
            return
        if current is not None and current.status is ConsumerStatus.REJECTED:
            if state.status is not ConsumerStatus.REJECTED:
                raise ConsumerStateError(
                    "REJECTED consumer state cannot be overwritten"
                )
            return
        _write_state_file(self._state_path(state.ingestion_key), state)

    def _state_path(self, ingestion_key: str) -> Path:
        return self.state_directory / _state_filename(ingestion_key)

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ConsumerError("consumer clock must return a timezone-aware time")
        return now.astimezone(timezone.utc)
