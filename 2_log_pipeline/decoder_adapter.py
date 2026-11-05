"""Adapt normalized event payloads for downstream AI analysis."""

import json

from normalized_event import NormalizedEvent


def payload_for_ai(event: NormalizedEvent) -> str:
    """Serialize JSONL payloads without applying heuristic decoding."""
    if event.source_format != "jsonl":
        raise ValueError(
            f"No payload preparation policy is defined for "
            f"'{event.source_format}' events"
        )

    return json.dumps(
        event.payload_raw,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
