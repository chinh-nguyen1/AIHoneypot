"""Parser for JSON Lines records written by the PHP request logger."""

import json
import re
from datetime import datetime
from typing import Any, Optional

from normalized_event import JSONParams, NormalizedEvent
from parser_contract import LogParseError


class JSONLParseError(LogParseError):
    """Raised when a JSONL record is malformed or violates the logger schema."""


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def _require_string(record: dict, field: str) -> str:
    value = record[field]
    if not isinstance(value, str):
        raise JSONLParseError(f"Field '{field}' must be a string")
    return value


def _require_nullable_string(record: dict, field: str) -> Optional[str]:
    value = record[field]
    if value is not None and not isinstance(value, str):
        raise JSONLParseError(f"Field '{field}' must be a string or null")
    return value


def _require_params(value: Any, field: str) -> JSONParams:
    if not isinstance(value, (dict, list)):
        raise JSONLParseError(f"Field '{field}' must be a JSON object or array")
    return value


def _json_values_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _json_values_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_values_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    return left == right


def _validate_timestamp(timestamp: str) -> None:
    try:
        normalized = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise JSONLParseError(
            "Field 'timestamp' must be a valid ISO 8601 datetime with a timezone"
        ) from exc

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise JSONLParseError(
            "Field 'timestamp' must include a timezone (UTC or an offset)"
        )


def parse_jsonl_record(raw_record: str) -> NormalizedEvent:
    """Parse one complete PHP-logger JSONL record into a normalized event.

    Returns a NormalizedEvent on success. Raises JSONLParseError for any
    malformed JSON or record that does not match the logger's record schema.
    This function does not decode payload values or perform AI/DB operations.
    """
    if not isinstance(raw_record, str):
        raise JSONLParseError("Record must be a string")

    try:
        record = json.loads(raw_record, parse_constant=_reject_non_json_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise JSONLParseError(f"Invalid JSON record: {exc}") from exc

    if not isinstance(record, dict):
        raise JSONLParseError("JSON record must be an object")

    required_fields = (
        "source_event_id",
        "timestamp",
        "source_ip",
        "method",
        "uri",
        "query_params",
        "body_params",
        "payload",
        "user_agent",
        "request_type",
    )
    missing_fields = [field for field in required_fields if field not in record]
    if missing_fields:
        raise JSONLParseError(
            "Missing required field(s): " + ", ".join(missing_fields)
        )

    # This 32-character lowercase hexadecimal format is the current PHP
    # logger's ID contract, not a general requirement of JSONL.
    source_event_id = _require_string(record, "source_event_id")
    if re.fullmatch(r"[0-9a-f]{32}", source_event_id) is None:
        raise JSONLParseError(
            "Field 'source_event_id' must be a 32-character lowercase hexadecimal ID"
        )

    timestamp = _require_string(record, "timestamp")
    _validate_timestamp(timestamp)
    source_ip = _require_nullable_string(record, "source_ip")
    method = _require_string(record, "method")
    uri = _require_string(record, "uri")
    user_agent = _require_nullable_string(record, "user_agent")
    request_type = _require_string(record, "request_type")

    query_params = _require_params(record["query_params"], "query_params")
    body_params = _require_params(record["body_params"], "body_params")

    payload_raw = record["payload"]
    if not isinstance(payload_raw, dict):
        raise JSONLParseError("Field 'payload' must be a JSON object")
    nested_params = {}
    for field in ("query_params", "body_params"):
        if field not in payload_raw:
            raise JSONLParseError(f"Field 'payload' is missing '{field}'")
        nested_params[field] = _require_params(
            payload_raw[field], f"payload.{field}"
        )

    for field in ("query_params", "body_params"):
        outer_params = query_params if field == "query_params" else body_params
        if not _json_values_equal(outer_params, nested_params[field]):
            raise JSONLParseError(
                f"Field '{field}' does not match 'payload.{field}'"
            )

    return NormalizedEvent(
        source_format="jsonl",
        raw_record=raw_record,
        source_event_id=source_event_id,
        timestamp=timestamp,
        source_ip=source_ip,
        method=method,
        uri=uri,
        query_params=query_params,
        body_params=body_params,
        payload_raw=payload_raw,
        user_agent=user_agent,
        request_type=request_type,
        present_fields=frozenset(record),
    )


class JSONLParser:
    """Adapter exposing the JSONL parser through the shared parser contract."""

    def parse(self, raw_record: str) -> NormalizedEvent:
        return parse_jsonl_record(raw_record)
