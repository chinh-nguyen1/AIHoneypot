"""Strict, atomic persistence for the durable-ingestion checkpoint.

Callers must persist and verify the corresponding Inbox event before writing a
checkpoint. This module accepts an explicit checkpoint value but cannot prove
that an Inbox event is durable or matches it.
"""

import errno
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, Optional, Union

from durability_contract import (
    DirectorySyncState,
    DurabilityError,
    DurabilityOutcome,
    PublicationState,
)
from inbox_record import InboxRecordError, create_ingestion_key


CHECKPOINT_VERSION = 2
EMPTY_SOURCE_PREFIX_SHA256 = hashlib.sha256(b"").hexdigest()
_SOURCE_PREFIX_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_CHECKPOINT_V1_FIELDS = {
    "version",
    "source_path",
    "source_file_identity",
    "next_byte_offset",
    "last_ingestion_key",
    "updated_at",
}
_CHECKPOINT_FIELDS = {
    "version",
    "source_path",
    "source_file_identity",
    "next_byte_offset",
    "last_ingestion_key",
    "updated_at",
    "source_prefix_sha256",
}
_UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS = {
    errno.EINVAL,
    getattr(errno, "ENOTSUP", errno.EINVAL),
    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
}
PathLike = Union[str, os.PathLike]


class CheckpointError(RuntimeError):
    """Base exception for checkpoint loading and persistence failures."""


class CheckpointValidationError(CheckpointError, ValueError):
    """Raised when a checkpoint value violates the versioned data contract."""


class CheckpointCorruptionError(CheckpointError):
    """Raised when a checkpoint file cannot be parsed or validated."""


class CheckpointSourceError(CheckpointError):
    """Raised when the requested source prefix cannot be read completely."""


class LegacyCheckpointError(CheckpointError):
    """Raised when a valid legacy checkpoint requires explicit migration."""


class CheckpointNotFoundError(CheckpointError, FileNotFoundError):
    """Raised when the requested checkpoint file does not exist."""


class CheckpointWriteError(DurabilityError, CheckpointError):
    """Checkpoint write failure with explicit publication and sync outcome."""

    def __init__(
        self,
        message: str,
        *,
        destination: Path,
        publication_state: PublicationState,
        directory_sync_state: DirectorySyncState,
        original_cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(
            message,
            operation="checkpoint_write",
            publication_state=publication_state,
            directory_sync_state=directory_sync_state,
            destination=destination,
            original_cause=original_cause,
        )

    @property
    def replacement_completed(self) -> bool:
        """Compatibility alias for whether os.replace published the file."""
        return self.published


@dataclass(frozen=True)
class Checkpoint:
    version: int
    source_path: str
    source_file_identity: str
    next_byte_offset: int
    last_ingestion_key: Optional[str]
    updated_at: str
    source_prefix_sha256: str

    def __post_init__(self) -> None:
        _validate_checkpoint(self)

    def to_json(self) -> str:
        return checkpoint_to_json(self)


@dataclass(frozen=True)
class CheckpointWriteResult:
    path: Path
    directory_fsynced: bool
    durability_note: Optional[str] = None
    outcome: Optional[DurabilityOutcome] = None


def hash_source_prefix(
    source_file,
    byte_offset: int,
    *,
    chunk_size: int = 1024 * 1024,
) -> tuple[str, bytes, Any]:
    """Hash exactly the raw source bytes in ``[0, byte_offset)``."""
    if type(byte_offset) is not int or byte_offset < 0:
        raise CheckpointValidationError(
            "source prefix offset must be a non-negative integer"
        )
    if type(chunk_size) is not int or chunk_size < 1:
        raise ValueError("chunk_size must be a positive integer")

    digest = hashlib.sha256()
    last_byte = b""
    source_file.seek(0)
    remaining = byte_offset
    while remaining:
        chunk = source_file.read(min(chunk_size, remaining))
        if not chunk:
            raise CheckpointSourceError(
                "source ended before the requested checkpoint offset"
            )
        if not isinstance(chunk, bytes) or len(chunk) > remaining:
            raise CheckpointSourceError(
                "source returned invalid bytes for the requested prefix"
            )
        digest.update(chunk)
        last_byte = chunk[-1:]
        remaining -= len(chunk)
    return digest.hexdigest(), last_byte, digest


def _validate_ingestion_key(
    ingestion_key: str,
    source_file_identity: str,
    next_byte_offset: int,
) -> None:
    try:
        parsed = json.loads(ingestion_key)
    except (json.JSONDecodeError, TypeError) as exc:
        raise CheckpointValidationError("last_ingestion_key is invalid") from exc

    if not isinstance(parsed, list) or len(parsed) != 3:
        raise CheckpointValidationError("last_ingestion_key is invalid")
    generation, start, end = parsed
    try:
        canonical_key = create_ingestion_key(generation, start, end)
    except (InboxRecordError, TypeError) as exc:
        raise CheckpointValidationError("last_ingestion_key is invalid") from exc

    if canonical_key != ingestion_key:
        raise CheckpointValidationError("last_ingestion_key is not canonical")
    if generation != source_file_identity or end != next_byte_offset:
        raise CheckpointValidationError(
            "last_ingestion_key does not match checkpoint identity and offset"
        )


def _validate_checkpoint(checkpoint: Checkpoint) -> None:
    if type(checkpoint.version) is not int or checkpoint.version != CHECKPOINT_VERSION:
        raise CheckpointValidationError("unsupported checkpoint version")
    if (
        not isinstance(checkpoint.source_path, str)
        or not checkpoint.source_path.strip()
    ):
        raise CheckpointValidationError("source_path must be a non-empty path")
    if not (
        PurePosixPath(checkpoint.source_path).is_absolute()
        or PureWindowsPath(checkpoint.source_path).is_absolute()
    ):
        raise CheckpointValidationError("source_path must be absolute")
    if (
        not isinstance(checkpoint.source_file_identity, str)
        or not checkpoint.source_file_identity.strip()
    ):
        raise CheckpointValidationError(
            "source_file_identity must be a non-empty string"
        )
    if type(checkpoint.next_byte_offset) is not int or checkpoint.next_byte_offset < 0:
        raise CheckpointValidationError(
            "next_byte_offset must be a non-negative integer"
        )
    if (
        not isinstance(checkpoint.source_prefix_sha256, str)
        or _SOURCE_PREFIX_SHA256_PATTERN.fullmatch(
            checkpoint.source_prefix_sha256
        )
        is None
    ):
        raise CheckpointValidationError(
            "source_prefix_sha256 must be 64 lowercase hexadecimal characters"
        )
    if (
        checkpoint.next_byte_offset == 0
        and checkpoint.source_prefix_sha256 != EMPTY_SOURCE_PREFIX_SHA256
    ):
        raise CheckpointValidationError(
            "an empty source prefix must use the SHA-256 empty digest"
        )
    if checkpoint.last_ingestion_key is not None:
        if not isinstance(checkpoint.last_ingestion_key, str):
            raise CheckpointValidationError(
                "last_ingestion_key must be a string or null"
            )
        _validate_ingestion_key(
            checkpoint.last_ingestion_key,
            checkpoint.source_file_identity,
            checkpoint.next_byte_offset,
        )
    if not isinstance(checkpoint.updated_at, str) or not checkpoint.updated_at:
        raise CheckpointValidationError(
            "updated_at must be a timezone-aware ISO 8601 timestamp"
        )
    try:
        timestamp = checkpoint.updated_at
        normalized = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise CheckpointValidationError(
            "updated_at must be a timezone-aware ISO 8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CheckpointValidationError(
            "updated_at must include timezone information"
        )


def checkpoint_to_dict(checkpoint: Checkpoint) -> Dict[str, Any]:
    """Return the validated checkpoint fields using the version 2 schema."""
    if not isinstance(checkpoint, Checkpoint):
        raise CheckpointValidationError("checkpoint must be a Checkpoint instance")
    _validate_checkpoint(checkpoint)
    return {
        "version": checkpoint.version,
        "source_path": checkpoint.source_path,
        "source_file_identity": checkpoint.source_file_identity,
        "next_byte_offset": checkpoint.next_byte_offset,
        "last_ingestion_key": checkpoint.last_ingestion_key,
        "updated_at": checkpoint.updated_at,
        "source_prefix_sha256": checkpoint.source_prefix_sha256,
    }


def checkpoint_to_json(checkpoint: Checkpoint) -> str:
    """Serialize a checkpoint deterministically as UTF-8-compatible JSON."""
    return json.dumps(
        checkpoint_to_dict(checkpoint),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON value: {value}")


def _reject_duplicate_fields(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CheckpointCorruptionError(
                f"checkpoint contains duplicate field: {key}"
            )
        result[key] = value
    return result


def checkpoint_from_json(serialized: str) -> Checkpoint:
    """Parse and strictly validate a serialized checkpoint."""
    if not isinstance(serialized, str):
        raise CheckpointCorruptionError("checkpoint content must be text")
    try:
        data = json.loads(
            serialized,
            object_pairs_hook=_reject_duplicate_fields,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, CheckpointCorruptionError):
            raise
        raise CheckpointCorruptionError("checkpoint contains invalid JSON") from exc
    if not isinstance(data, dict):
        raise CheckpointCorruptionError("checkpoint JSON must be an object")
    if (
        type(data.get("version")) is int
        and data["version"] == 1
        and set(data) == _CHECKPOINT_V1_FIELDS
    ):
        raise LegacyCheckpointError(
            "checkpoint version 1 has no source-prefix digest; "
            "explicit operator-approved migration is required"
        )
    if set(data) != _CHECKPOINT_FIELDS:
        raise CheckpointCorruptionError(
            "checkpoint fields do not match the version 2 schema"
        )
    try:
        return Checkpoint(**data)
    except (TypeError, CheckpointValidationError) as exc:
        raise CheckpointCorruptionError(
            "checkpoint fields failed validation"
        ) from exc


def load_checkpoint(checkpoint_path: PathLike) -> Checkpoint:
    """Load one checkpoint file without initializing or repairing state."""
    path = Path(checkpoint_path)
    try:
        content = path.read_bytes()
    except FileNotFoundError as exc:
        raise CheckpointNotFoundError("checkpoint file does not exist") from exc
    except OSError as exc:
        raise CheckpointError("checkpoint file could not be read") from exc

    try:
        serialized = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CheckpointCorruptionError("checkpoint is not valid UTF-8") from exc
    return checkpoint_from_json(serialized)


def _write_temp_file(file_descriptor: int, content: bytes) -> None:
    with os.fdopen(file_descriptor, "wb", closefd=False) as checkpoint_file:
        written = checkpoint_file.write(content)
        if written != len(content):
            raise OSError("incomplete checkpoint write")
        checkpoint_file.flush()
        os.fsync(checkpoint_file.fileno())


def _fsync_directory(directory: Path) -> None:
    if getattr(os, "name", "") == "nt":
        raise NotImplementedError("directory fsync is unsupported on Windows")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(directory, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _directory_fsync_is_unsupported(error: OSError) -> bool:
    return error.errno in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS


def write_checkpoint(
    checkpoint_path: PathLike,
    checkpoint: Checkpoint,
    *,
    create_directories: bool = True,
) -> CheckpointWriteResult:
    """Atomically persist an explicit checkpoint value.

    The caller must first confirm the corresponding Inbox event has been
    durably persisted and matches this checkpoint. This function does not
    access or verify the Inbox, and never computes or advances an offset.
    """
    destination = Path(checkpoint_path)
    directory = destination.parent

    try:
        content = (checkpoint_to_json(checkpoint) + "\n").encode("utf-8")
    except CheckpointValidationError:
        raise
    except (TypeError, UnicodeError, RecursionError, ValueError) as exc:
        raise CheckpointWriteError(
            "failed to serialize checkpoint",
            destination=destination,
            publication_state=PublicationState.NOT_PUBLISHED,
            directory_sync_state=DirectorySyncState.NOT_ATTEMPTED,
            original_cause=exc,
        ) from exc

    if create_directories:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CheckpointWriteError(
                "failed to create checkpoint directory",
                destination=destination,
                publication_state=PublicationState.NOT_PUBLISHED,
                directory_sync_state=DirectorySyncState.NOT_ATTEMPTED,
                original_cause=exc,
            ) from exc

    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=directory,
        )
    except OSError as exc:
        raise CheckpointWriteError(
            "failed to create temporary checkpoint",
            destination=destination,
            publication_state=PublicationState.NOT_PUBLISHED,
            directory_sync_state=DirectorySyncState.NOT_ATTEMPTED,
            original_cause=exc,
        ) from exc

    temporary_path = Path(temporary_name)
    descriptor_open = True
    try:
        try:
            _write_temp_file(descriptor, content)
            descriptor_open = False
            os.close(descriptor)
        except OSError as exc:
            raise CheckpointWriteError(
                "failed to write and sync temporary checkpoint",
                destination=destination,
                publication_state=PublicationState.NOT_PUBLISHED,
                directory_sync_state=DirectorySyncState.NOT_ATTEMPTED,
                original_cause=exc,
            ) from exc

        try:
            os.replace(temporary_path, destination)
        except OSError as exc:
            raise CheckpointWriteError(
                "failed to atomically replace checkpoint",
                destination=destination,
                publication_state=PublicationState.NOT_PUBLISHED,
                directory_sync_state=DirectorySyncState.NOT_ATTEMPTED,
                original_cause=exc,
            ) from exc

        try:
            _fsync_directory(directory)
        except NotImplementedError as exc:
            raise CheckpointWriteError(
                "checkpoint replaced but directory fsync is unsupported",
                destination=destination,
                publication_state=PublicationState.PUBLISHED_UNCONFIRMED,
                directory_sync_state=DirectorySyncState.UNSUPPORTED,
                original_cause=exc,
            ) from exc
        except OSError as exc:
            if _directory_fsync_is_unsupported(exc):
                raise CheckpointWriteError(
                    "checkpoint replaced but directory fsync is unsupported",
                    destination=destination,
                    publication_state=PublicationState.PUBLISHED_UNCONFIRMED,
                    directory_sync_state=DirectorySyncState.UNSUPPORTED,
                    original_cause=exc,
                ) from exc
            raise CheckpointWriteError(
                "checkpoint replaced, but directory fsync failed",
                destination=destination,
                publication_state=PublicationState.PUBLISHED_UNCONFIRMED,
                directory_sync_state=DirectorySyncState.FAILED,
                original_cause=exc,
            ) from exc

        outcome = DurabilityOutcome(
            operation="checkpoint_write",
            publication_state=PublicationState.DURABLE,
            directory_sync_state=DirectorySyncState.SYNCED,
            destination=destination,
        )
        return CheckpointWriteResult(
            path=destination,
            directory_fsynced=True,
            durability_note=None,
            outcome=outcome,
        )
    finally:
        if descriptor_open:
            os.close(descriptor)


def create_checkpoint_if_absent(
    checkpoint_path: PathLike,
    checkpoint: Checkpoint,
) -> CheckpointWriteResult:
    """Create one checkpoint with exclusive creation; never replace a file."""
    destination = Path(checkpoint_path)
    directory = destination.parent

    try:
        content = (checkpoint_to_json(checkpoint) + "\n").encode("utf-8")
    except CheckpointValidationError:
        raise
    except (TypeError, UnicodeError, RecursionError, ValueError) as exc:
        raise CheckpointWriteError(
            "failed to serialize checkpoint",
            destination=destination,
            publication_state=PublicationState.NOT_PUBLISHED,
            directory_sync_state=DirectorySyncState.NOT_ATTEMPTED,
            original_cause=exc,
        ) from exc

    required_flags = ("O_CREAT", "O_EXCL", "O_WRONLY")
    if any(not hasattr(os, flag) for flag in required_flags):
        raise CheckpointWriteError(
            "exclusive checkpoint creation is unsupported on this platform",
            destination=destination,
            publication_state=PublicationState.NOT_PUBLISHED,
            directory_sync_state=DirectorySyncState.NOT_ATTEMPTED,
        )

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(destination, flags, 0o600)
    except OSError as exc:
        raise CheckpointWriteError(
            "failed to create checkpoint exclusively",
            destination=destination,
            publication_state=PublicationState.NOT_PUBLISHED,
            directory_sync_state=DirectorySyncState.NOT_ATTEMPTED,
            original_cause=exc,
        ) from exc

    descriptor_open = True
    try:
        try:
            _write_temp_file(descriptor, content)
            os.close(descriptor)
            descriptor_open = False
        except OSError as exc:
            raise CheckpointWriteError(
                "checkpoint was created but could not be completely written and synced",
                destination=destination,
                publication_state=PublicationState.PUBLISHED_UNCONFIRMED,
                directory_sync_state=DirectorySyncState.NOT_ATTEMPTED,
                original_cause=exc,
            ) from exc

        try:
            _fsync_directory(directory)
        except NotImplementedError as exc:
            raise CheckpointWriteError(
                "checkpoint was created but directory fsync is unsupported",
                destination=destination,
                publication_state=PublicationState.PUBLISHED_UNCONFIRMED,
                directory_sync_state=DirectorySyncState.UNSUPPORTED,
                original_cause=exc,
            ) from exc
        except OSError as exc:
            if _directory_fsync_is_unsupported(exc):
                raise CheckpointWriteError(
                    "checkpoint was created but directory fsync is unsupported",
                    destination=destination,
                    publication_state=PublicationState.PUBLISHED_UNCONFIRMED,
                    directory_sync_state=DirectorySyncState.UNSUPPORTED,
                    original_cause=exc,
                ) from exc
            raise CheckpointWriteError(
                "checkpoint was created but directory fsync failed",
                destination=destination,
                publication_state=PublicationState.PUBLISHED_UNCONFIRMED,
                directory_sync_state=DirectorySyncState.FAILED,
                original_cause=exc,
            ) from exc

        outcome = DurabilityOutcome(
            operation="checkpoint_create",
            publication_state=PublicationState.DURABLE,
            directory_sync_state=DirectorySyncState.SYNCED,
            destination=destination,
        )
        return CheckpointWriteResult(
            path=destination,
            directory_fsynced=True,
            durability_note=None,
            outcome=outcome,
        )
    finally:
        if descriptor_open:
            os.close(descriptor)
