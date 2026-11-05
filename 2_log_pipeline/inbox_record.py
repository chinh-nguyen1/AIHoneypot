"""Immutable inbox event records and their stable ingestion identifiers."""

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional


INBOX_RECORD_VERSION = 1
_REQUIRED_FIELDS = {
    "version",
    "ingestion_key",
    "source_event_id",
    "source_format",
    "raw_record",
    "raw_record_bytes_b64",
    "source_file_identity",
    "byte_offset_start",
    "byte_offset_end",
    "ingested_at",
    "parse_state",
    "parse_error",
    "raw_bytes_sha256",
}


class InboxRecordError(ValueError):
    """Raised when an inbox record or its source metadata is invalid."""


class ParseState(str, Enum):
    UNVALIDATED = "UNVALIDATED"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True)
class InboxEvent:
    version: int
    ingestion_key: str
    source_event_id: Optional[str]
    source_format: str
    raw_record: Optional[str]
    raw_record_bytes_b64: str
    source_file_identity: str
    byte_offset_start: int
    byte_offset_end: int
    ingested_at: str
    parse_state: ParseState
    parse_error: Optional[str]
    raw_bytes_sha256: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "ingestion_key": self.ingestion_key,
            "source_event_id": self.source_event_id,
            "source_format": self.source_format,
            "raw_record": self.raw_record,
            "raw_record_bytes_b64": self.raw_record_bytes_b64,
            "source_file_identity": self.source_file_identity,
            "byte_offset_start": self.byte_offset_start,
            "byte_offset_end": self.byte_offset_end,
            "ingested_at": self.ingested_at,
            "parse_state": self.parse_state.value,
            "parse_error": self.parse_error,
            "raw_bytes_sha256": self.raw_bytes_sha256,
        }

    def to_json(self) -> str:
        return inbox_event_to_json(self)


def _validate_generation_and_offsets(
    source_file_generation: str,
    byte_offset_start: int,
    byte_offset_end: int,
) -> None:
    if not isinstance(source_file_generation, str) or not source_file_generation:
        raise InboxRecordError("source_file_generation must be a non-empty string")
    if type(byte_offset_start) is not int or byte_offset_start < 0:
        raise InboxRecordError("byte_offset_start must be a non-negative integer")
    if type(byte_offset_end) is not int or byte_offset_end < 0:
        raise InboxRecordError("byte_offset_end must be a non-negative integer")
    if byte_offset_end <= byte_offset_start:
        raise InboxRecordError("byte_offset_end must be greater than byte_offset_start")


def create_ingestion_key(
    source_file_generation: str,
    byte_offset_start: int,
    byte_offset_end: int,
) -> str:
    """Return an unambiguous key for one byte range in a source generation."""
    _validate_generation_and_offsets(
        source_file_generation, byte_offset_start, byte_offset_end
    )
    return json.dumps(
        [source_file_generation, byte_offset_start, byte_offset_end],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def inbox_event_filename(ingestion_key: str) -> str:
    if not isinstance(ingestion_key, str) or not ingestion_key:
        raise InboxRecordError("ingestion_key must be a non-empty string")
    digest = hashlib.sha256(ingestion_key.encode("utf-8")).hexdigest()
    return f"{digest}.event.json"


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant: {value}")


def _classify_raw_bytes(
    raw_record_bytes: bytes,
) -> tuple[Optional[str], ParseState, Optional[str]]:
    try:
        raw_record = raw_record_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        return (
            None,
            ParseState.QUARANTINED,
            f"Invalid UTF-8 at byte {exc.start}: {exc.reason}",
        )

    try:
        json.loads(raw_record, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        return raw_record, ParseState.QUARANTINED, f"Invalid JSON: {exc}"

    return raw_record, ParseState.UNVALIDATED, None


def _validate_timestamp(value: str) -> None:
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise InboxRecordError("ingested_at must be a timezone-aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InboxRecordError("ingested_at must be a timezone-aware ISO timestamp")


def create_inbox_event(
    source_file_generation: str,
    byte_offset_start: int,
    byte_offset_end: int,
    raw_record_bytes: bytes,
    *,
    source_event_id: Optional[str] = None,
    source_format: str = "jsonl",
    ingested_at: Optional[str] = None,
) -> InboxEvent:
    """Build an event without changing or normalizing the source bytes."""
    ingestion_key = create_ingestion_key(
        source_file_generation, byte_offset_start, byte_offset_end
    )
    if type(raw_record_bytes) is not bytes:
        raise InboxRecordError("raw_record_bytes must be bytes")
    if len(raw_record_bytes) != byte_offset_end - byte_offset_start:
        raise InboxRecordError("byte range length must match raw_record_bytes length")
    if source_event_id is not None and not isinstance(source_event_id, str):
        raise InboxRecordError("source_event_id must be a string or null")
    if not isinstance(source_format, str) or not source_format:
        raise InboxRecordError("source_format must be a non-empty string")

    timestamp = ingested_at or datetime.now(timezone.utc).isoformat()
    if not isinstance(timestamp, str) or not timestamp:
        raise InboxRecordError("ingested_at must be a non-empty string")
    _validate_timestamp(timestamp)

    raw_record, parse_state, parse_error = _classify_raw_bytes(raw_record_bytes)
    return InboxEvent(
        version=INBOX_RECORD_VERSION,
        ingestion_key=ingestion_key,
        source_event_id=source_event_id,
        source_format=source_format,
        raw_record=raw_record,
        raw_record_bytes_b64=base64.b64encode(raw_record_bytes).decode("ascii"),
        source_file_identity=source_file_generation,
        byte_offset_start=byte_offset_start,
        byte_offset_end=byte_offset_end,
        ingested_at=timestamp,
        parse_state=parse_state,
        parse_error=parse_error,
        raw_bytes_sha256=hashlib.sha256(raw_record_bytes).hexdigest(),
    )


def inbox_event_to_json(event: InboxEvent) -> str:
    if not isinstance(event, InboxEvent):
        raise InboxRecordError("event must be an InboxEvent")
    return json.dumps(
        event.to_dict(),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InboxRecordError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def inbox_event_from_json(serialized: str) -> InboxEvent:
    """Deserialize and validate an event without repairing inconsistent data."""
    if not isinstance(serialized, str):
        raise InboxRecordError("serialized event must be a string")
    try:
        data = json.loads(
            serialized,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, InboxRecordError):
            raise
        raise InboxRecordError(f"Invalid inbox JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise InboxRecordError("inbox JSON must be an object")
    if set(data) != _REQUIRED_FIELDS:
        raise InboxRecordError("inbox JSON fields do not match the version 1 contract")

    version = data["version"]
    if type(version) is not int or version != INBOX_RECORD_VERSION:
        raise InboxRecordError("unsupported inbox record version")

    source_file_identity = data["source_file_identity"]
    start = data["byte_offset_start"]
    end = data["byte_offset_end"]
    _validate_generation_and_offsets(source_file_identity, start, end)
    expected_key = create_ingestion_key(source_file_identity, start, end)
    if data["ingestion_key"] != expected_key:
        raise InboxRecordError("ingestion_key does not match source identity and offsets")

    source_event_id = data["source_event_id"]
    if source_event_id is not None and not isinstance(source_event_id, str):
        raise InboxRecordError("source_event_id must be a string or null")
    source_format = data["source_format"]
    if not isinstance(source_format, str) or not source_format:
        raise InboxRecordError("source_format must be a non-empty string")
    ingested_at = data["ingested_at"]
    if not isinstance(ingested_at, str) or not ingested_at:
        raise InboxRecordError("ingested_at must be a non-empty string")
    _validate_timestamp(ingested_at)

    encoded_bytes = data["raw_record_bytes_b64"]
    if not isinstance(encoded_bytes, str):
        raise InboxRecordError("raw_record_bytes_b64 must be a string")
    try:
        raw_bytes = base64.b64decode(encoded_bytes, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InboxRecordError("raw_record_bytes_b64 is invalid") from exc
    if len(raw_bytes) != end - start:
        raise InboxRecordError("byte range length does not match decoded raw bytes")

    checksum = data["raw_bytes_sha256"]
    if not isinstance(checksum, str) or re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
        raise InboxRecordError("raw_bytes_sha256 must be a lowercase SHA-256 hex digest")
    if hashlib.sha256(raw_bytes).hexdigest() != checksum:
        raise InboxRecordError("raw_bytes_sha256 does not match decoded raw bytes")

    expected_raw_record, expected_parse_state, expected_parse_error = (
        _classify_raw_bytes(raw_bytes)
    )
    raw_record = data["raw_record"]
    if raw_record != expected_raw_record:
        raise InboxRecordError("raw_record does not match decoded raw bytes")
    try:
        parse_state = ParseState(data["parse_state"])
    except (TypeError, ValueError) as exc:
        raise InboxRecordError("parse_state is invalid") from exc
    if parse_state is not expected_parse_state:
        raise InboxRecordError("parse_state does not match raw bytes")
    parse_error = data["parse_error"]
    if expected_parse_error is None:
        if parse_error is not None:
            raise InboxRecordError("parse_error must be null for valid JSON")
    elif not isinstance(parse_error, str) or not parse_error:
        raise InboxRecordError("parse_error is required for quarantined raw bytes")

    return InboxEvent(
        version=version,
        ingestion_key=expected_key,
        source_event_id=source_event_id,
        source_format=source_format,
        raw_record=raw_record,
        raw_record_bytes_b64=encoded_bytes,
        source_file_identity=source_file_identity,
        byte_offset_start=start,
        byte_offset_end=end,
        ingested_at=ingested_at,
        parse_state=parse_state,
        parse_error=parse_error,
        raw_bytes_sha256=checksum,
    )
