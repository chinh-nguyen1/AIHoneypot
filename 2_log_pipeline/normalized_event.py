"""Shared data types for events normalized from different log formats."""

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Union


JSONScalar = Union[str, int, float, bool, None]
JSONValue = Union[JSONScalar, Dict[str, "JSONValue"], List["JSONValue"]]
JSONParams = Union[Dict[str, JSONValue], List[JSONValue]]


@dataclass(frozen=True)
class NormalizedEvent:
    source_format: str
    raw_record: str
    source_event_id: Optional[str] = None
    timestamp: Optional[str] = None
    source_ip: Optional[str] = None
    method: Optional[str] = None
    uri: Optional[str] = None
    query_params: Optional[JSONParams] = None
    body_params: Optional[JSONParams] = None
    payload_raw: Optional[JSONValue] = None
    payload_decoded: Optional[str] = None
    user_agent: Optional[str] = None
    request_type: Optional[str] = None
    present_fields: FrozenSet[str] = field(default_factory=frozenset)
