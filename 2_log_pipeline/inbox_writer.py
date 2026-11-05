"""Atomic, no-clobber persistence for durable inbox event files."""

import base64
import errno
import os
import stat
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Union

from inbox_record import (
    InboxEvent,
    InboxRecordError,
    inbox_event_filename,
    inbox_event_from_json,
    inbox_event_to_json,
)
from durability_contract import (
    DirectorySyncState,
    DurabilityError,
    DurabilityOutcome,
    PublicationState,
)


PathLike = Union[str, os.PathLike]


class InboxWriteError(RuntimeError):
    """Raised when an inbox event cannot be safely persisted or verified."""


class InboxEventConflictError(InboxWriteError):
    """Raised when an existing event file conflicts with the requested event."""


class InboxWriteStatus(str, Enum):
    CREATED = "created"
    ALREADY_EXISTS = "already_exists"


@dataclass(frozen=True)
class InboxWriteResult:
    path: Path
    status: InboxWriteStatus
    outcome: DurabilityOutcome

    @property
    def already_exists(self) -> bool:
        return self.status is InboxWriteStatus.ALREADY_EXISTS


class InboxCleanupError(DurabilityError):
    """Raised when post-durability temporary-file cleanup fails."""

    def __init__(
        self,
        message: str,
        *,
        destination: Path,
        temporary_path: Path,
        cleanup_stage: str,
        original_cause: BaseException,
    ) -> None:
        super().__init__(
            message,
            operation="inbox_write",
            publication_state=PublicationState.DURABLE,
            directory_sync_state=DirectorySyncState.SYNCED,
            destination=destination,
            original_cause=original_cause,
        )
        self.temporary_path = temporary_path
        self.cleanup_stage = cleanup_stage


def _fsync_directory(directory: Path) -> None:
    if getattr(os, "name", "") == "nt":
        raise NotImplementedError("directory fsync is unsupported on Windows")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(directory, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_temp_file(file_descriptor: int, content: bytes) -> None:
    with os.fdopen(file_descriptor, "wb", closefd=False) as temp_file:
        written = temp_file.write(content)
        if written != len(content):
            raise OSError("incomplete write while creating inbox event")
        temp_file.flush()
        os.fsync(temp_file.fileno())


def _publish_no_replace(temp_path: Path, destination: Path) -> bool:
    """Atomically publish without clobbering and return whether temp is linked."""
    if getattr(os, "name", "") == "nt":
        os.rename(temp_path, destination)
        return False
    os.link(temp_path, destination)
    return True


def _directory_sync_state(error: BaseException) -> DirectorySyncState:
    if isinstance(error, NotImplementedError):
        return DirectorySyncState.UNSUPPORTED
    if isinstance(error, OSError) and error.errno in {
        errno.EINVAL,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    }:
        return DirectorySyncState.UNSUPPORTED
    return DirectorySyncState.FAILED


def _sync_published_directory(directory: Path, destination: Path) -> None:
    try:
        _fsync_directory(directory)
    except (NotImplementedError, OSError) as exc:
        sync_state = _directory_sync_state(exc)
        raise DurabilityError(
            "inbox event published but directory durability was not confirmed",
            operation="inbox_write",
            publication_state=PublicationState.PUBLISHED_UNCONFIRMED,
            directory_sync_state=sync_state,
            destination=destination,
            original_cause=exc,
        ) from exc


def _write_failure(
    message: str,
    destination: Path,
    cause: OSError,
) -> DurabilityError:
    return DurabilityError(
        message,
        operation="inbox_write",
        publication_state=PublicationState.NOT_PUBLISHED,
        directory_sync_state=DirectorySyncState.NOT_ATTEMPTED,
        destination=destination,
        original_cause=cause,
    )


def _durable_outcome(destination: Path) -> DurabilityOutcome:
    return DurabilityOutcome(
        operation="inbox_write",
        publication_state=PublicationState.DURABLE,
        directory_sync_state=DirectorySyncState.SYNCED,
        destination=destination,
    )


def _read_existing_event(path: Path) -> InboxEvent:
    try:
        file_stat = path.lstat()
        if not stat.S_ISREG(file_stat.st_mode):
            raise InboxEventConflictError(
                f"existing inbox destination is not a regular file: {path}"
            )
        serialized = path.read_text(encoding="utf-8")
        return inbox_event_from_json(serialized)
    except InboxEventConflictError:
        raise
    except (OSError, UnicodeError, InboxRecordError) as exc:
        raise InboxEventConflictError(
            f"existing inbox event is unreadable or invalid: {path}"
        ) from exc


def _verify_existing_event(path: Path, requested: InboxEvent) -> None:
    existing = _read_existing_event(path)
    try:
        existing_raw = base64.b64decode(existing.raw_record_bytes_b64, validate=True)
        requested_raw = base64.b64decode(
            requested.raw_record_bytes_b64, validate=True
        )
    except (ValueError, TypeError) as exc:
        raise InboxEventConflictError(
            f"inbox event contains invalid raw bytes: {path}"
        ) from exc

    if (
        existing.ingestion_key != requested.ingestion_key
        or existing.raw_bytes_sha256 != requested.raw_bytes_sha256
        or existing_raw != requested_raw
    ):
        raise InboxEventConflictError(
            f"existing inbox event conflicts with requested event: {path}"
        )


def _validate_event(event: InboxEvent) -> bytes:
    serialized = inbox_event_to_json(event)
    validated = inbox_event_from_json(serialized)
    if validated != event:
        raise InboxWriteError("event does not round-trip through the inbox contract")
    return serialized.encode("utf-8")


def _existing_result(
    path: Path, event: InboxEvent, directory: Path
) -> InboxWriteResult:
    _verify_existing_event(path, event)
    _sync_published_directory(directory, path)
    return InboxWriteResult(
        path=path,
        status=InboxWriteStatus.ALREADY_EXISTS,
        outcome=_durable_outcome(path),
    )


def write_inbox_event(inbox_directory: PathLike, event: InboxEvent) -> InboxWriteResult:
    """Publish and durability-check an event without replacing its stable name.

    A same-directory hard link is used as the atomic no-clobber publication
    primitive because os.rename/os.replace do not provide portable
    rename-if-absent semantics. Only a result carrying DURABLE/SYNCED is a
    successful acknowledgment. A DurabilityError may indicate publication
    already occurred; inspect its outcome before retrying.
    """
    content = _validate_event(event)
    directory = Path(inbox_directory)
    destination = directory / inbox_event_filename(event.ingestion_key)

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _write_failure(
            "failed to create inbox directory",
            destination,
            exc,
        ) from exc

    if os.path.lexists(destination):
        return _existing_result(destination, event, directory)

    try:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=directory,
        )
    except OSError as exc:
        raise _write_failure(
            "failed to create temporary inbox event",
            destination,
            exc,
        ) from exc

    temp_path = Path(temp_name)
    descriptor_owned = True
    try:
        try:
            _write_temp_file(descriptor, content)
            descriptor_owned = False
            os.close(descriptor)
        except OSError as exc:
            raise _write_failure(
                "failed to write and sync temporary inbox event",
                destination,
                exc,
            ) from exc

        created_here = True
        try:
            temp_is_linked = _publish_no_replace(temp_path, destination)
        except OSError as exc:
            if not os.path.lexists(destination):
                raise _write_failure(
                    "failed to publish temporary inbox event",
                    destination,
                    exc,
                ) from exc
            _verify_existing_event(destination, event)
            created_here = False
            temp_is_linked = False

        _sync_published_directory(directory, destination)
        outcome = _durable_outcome(destination)

        if os.path.lexists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError as exc:
                raise InboxCleanupError(
                    "inbox event is durable but temporary-file cleanup failed",
                    destination=destination,
                    temporary_path=temp_path,
                    cleanup_stage="unlink_temp",
                    original_cause=exc,
                ) from exc

            if temp_is_linked or not created_here:
                try:
                    _fsync_directory(directory)
                except (NotImplementedError, OSError) as exc:
                    raise InboxCleanupError(
                        "inbox event is durable but cleanup directory sync failed",
                        destination=destination,
                        temporary_path=temp_path,
                        cleanup_stage="fsync_directory_after_temp_unlink",
                        original_cause=exc,
                    ) from exc

        return InboxWriteResult(
            path=destination,
            status=(
                InboxWriteStatus.CREATED
                if created_here
                else InboxWriteStatus.ALREADY_EXISTS
            ),
            outcome=outcome,
        )
    finally:
        if descriptor_owned:
            os.close(descriptor)
