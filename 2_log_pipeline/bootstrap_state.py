"""Explicit, fail-closed initialization of a new watcher checkpoint."""

import argparse
import errno
import json
import logging
import os
import re
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Literal, Optional, TypeVar, Union

from checkpoint import (
    CHECKPOINT_VERSION,
    Checkpoint,
    CheckpointError,
    CheckpointCorruptionError,
    CheckpointNotFoundError,
    CheckpointSourceError,
    LegacyCheckpointError,
    create_checkpoint_if_absent,
    hash_source_prefix,
    load_checkpoint,
)
from config import (
    CHECKPOINT_FILE_PATH,
    CONSUMER_LOCK_FILE_PATH,
    CONSUMER_STATE_DIRECTORY_PATH,
    INBOX_DIRECTORY_PATH,
    LOCK_FILE_PATH,
    LOG_FILE_PATH,
    REJECTED_FILE_PATH,
)
from durability_contract import DurabilityError
from single_writer_lock import SingleWriterLock, SingleWriterLockError


LOGGER = logging.getLogger(__name__)
PathLike = Union[str, os.PathLike]
BootstrapMode = Literal["from-start", "from-end"]


class BootstrapError(RuntimeError):
    """Raised when bootstrap cannot safely initialize a new checkpoint."""


class ExistingCheckpointError(BootstrapError):
    """Raised when a checkpoint already exists and must not be overwritten."""


class BootstrapInboxNotEmptyError(BootstrapError):
    """Raised when Inbox contents exist without a checkpoint."""


class SourceIdentityMismatchError(BootstrapError):
    """Raised when the source no longer matches an existing checkpoint."""


@dataclass(frozen=True)
class BootstrapResult:
    checkpoint: Checkpoint
    created: bool


class PipelineStateStatus(str, Enum):
    UNINITIALIZED = "UNINITIALIZED"
    LEGACY_VALID = "LEGACY_VALID"
    BOOTSTRAPPED_V2 = "BOOTSTRAPPED_V2"
    INCONSISTENT = "INCONSISTENT"
    CORRUPT = "CORRUPT"
    SOURCE_MISMATCH = "SOURCE_MISMATCH"


@dataclass(frozen=True)
class PipelineStateInspection:
    status: PipelineStateStatus
    details: tuple[str, ...]


_INBOX_EVENT_NAME = re.compile(r"[0-9a-f]{64}\.event\.json\Z")
_CONSUMER_STATE_NAME = re.compile(r"[0-9a-f]{64}\.json\Z")
_TEMPORARY_NAME = re.compile(r"\..+\..+\.tmp\Z")
_SOURCE_IO_RETRY_DELAYS = (0.25, 0.5, 1.0, 2.0)
_TRANSIENT_SOURCE_ERRNOS = {
    errno.EFAULT,
    errno.EINTR,
    errno.EAGAIN,
    getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
}
_Result = TypeVar("_Result")


def _retry_source_metadata(operation: Callable[[], _Result]) -> _Result:
    for attempt in range(len(_SOURCE_IO_RETRY_DELAYS) + 1):
        try:
            return operation()
        except OSError as exc:
            if exc.errno not in _TRANSIENT_SOURCE_ERRNOS:
                raise
            if attempt == len(_SOURCE_IO_RETRY_DELAYS):
                raise
            time.sleep(_SOURCE_IO_RETRY_DELAYS[attempt])
    raise AssertionError("source metadata retry loop exited unexpectedly")


def _lstat(path: Path, component: str, issues: list[str]):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        issues.append(
            f"{component}: metadata could not be inspected "
            f"({type(exc).__name__})"
        )
        return None


def _is_regular_file(file_stat: os.stat_result) -> bool:
    return stat.S_ISREG(file_stat.st_mode) and not stat.S_ISLNK(file_stat.st_mode)


def _is_directory(file_stat: os.stat_result) -> bool:
    return stat.S_ISDIR(file_stat.st_mode) and not stat.S_ISLNK(file_stat.st_mode)


def inspect_pipeline_state(
    *,
    source_path: PathLike = LOG_FILE_PATH,
    checkpoint_path: PathLike = CHECKPOINT_FILE_PATH,
    inbox_directory: PathLike = INBOX_DIRECTORY_PATH,
    consumer_state_directory: PathLike = CONSUMER_STATE_DIRECTORY_PATH,
    lock_path: PathLike = LOCK_FILE_PATH,
    consumer_lock_path: PathLike = CONSUMER_LOCK_FILE_PATH,
    rejected_file_path: PathLike = REJECTED_FILE_PATH,
    watcher_lock_owned: bool = False,
    reject_present_locks: bool = False,
) -> PipelineStateInspection:
    """Inspect Pipeline state without acquiring locks or modifying files.

    Checkpoint and Consumer sidecar contents are read only for validation.
    Inbox event contents and source log contents are never opened.
    """
    source = Path(source_path)
    checkpoint = Path(checkpoint_path)
    inbox = Path(inbox_directory)
    consumer_state = Path(consumer_state_directory)
    watcher_lock = Path(lock_path)
    consumer_lock = Path(consumer_lock_path)
    rejected_file = Path(rejected_file_path)

    issues: list[str] = []
    corrupt = False
    source_mismatch = False
    details: list[str] = []
    inbox_count = 0
    sidecar_counts = {"RETRY": 0, "REJECTED": 0, "PROCESSED": 0}
    has_consumer_state_directory = False
    has_persisted_state_without_checkpoint = False

    state_root = checkpoint.parent
    root_stat = _lstat(state_root, "state directory", issues)
    if root_stat is not None and not _is_directory(root_stat):
        issues.append("state directory: expected a real directory")
    elif root_stat is not None:
        expected_root_names = {
            path.name
            for path in (
                checkpoint,
                inbox,
                consumer_state,
                watcher_lock,
                consumer_lock,
                rejected_file,
            )
            if path.parent == state_root
        }
        try:
            root_entries = sorted(state_root.iterdir(), key=lambda entry: entry.name)
        except OSError as exc:
            root_entries = []
            issues.append(
                f"state directory: entries could not be inspected "
                f"({type(exc).__name__})"
            )
        for entry in root_entries:
            if entry.name in expected_root_names:
                continue
            if _TEMPORARY_NAME.fullmatch(entry.name):
                issues.append(f"temporary file: {entry.name}")
            else:
                issues.append(f"unrecognized state component: {entry.name}")

    checkpoint_stat = _lstat(checkpoint, "checkpoint", issues)
    checkpoint_value = None
    if checkpoint_stat is not None:
        if not _is_regular_file(checkpoint_stat):
            corrupt = True
            details.append("checkpoint: expected a regular non-symlink file")
        else:
            try:
                checkpoint_value = load_checkpoint(checkpoint)
            except CheckpointCorruptionError:
                corrupt = True
                details.append("checkpoint: content is corrupt or unsupported")
            except LegacyCheckpointError as exc:
                issues.append(f"checkpoint: {exc}")
            except CheckpointNotFoundError:
                checkpoint_value = None
            except CheckpointError as exc:
                issues.append(
                    f"checkpoint: could not be validated ({type(exc).__name__})"
                )
            else:
                details.append(
                    f"checkpoint: valid version {checkpoint_value.version}, "
                    f"offset {checkpoint_value.next_byte_offset}"
                )

    inbox_entries: list[Path] = []
    inbox_stat = _lstat(inbox, "Inbox", issues)
    inbox_exists = inbox_stat is not None
    if inbox_stat is not None:
        if not _is_directory(inbox_stat):
            issues.append("Inbox: expected a real directory")
        else:
            try:
                inbox_entries = sorted(inbox.iterdir(), key=lambda entry: entry.name)
            except OSError as exc:
                issues.append(
                    f"Inbox: entries could not be inspected ({type(exc).__name__})"
                )
    for entry in inbox_entries:
        entry_stat = _lstat(entry, "Inbox component", issues)
        if entry_stat is None:
            continue
        if _INBOX_EVENT_NAME.fullmatch(entry.name):
            if not _is_regular_file(entry_stat):
                issues.append(f"Inbox component {entry.name}: expected a regular file")
            else:
                inbox_count += 1
        elif _TEMPORARY_NAME.fullmatch(entry.name):
            issues.append(f"Inbox temporary component: {entry.name}")
        else:
            issues.append(f"unrecognized Inbox component: {entry.name}")
    if inbox_exists and inbox_stat is not None and _is_directory(inbox_stat):
        details.append(f"Inbox: directory, {inbox_count} event file(s)")

    consumer_entries: list[Path] = []
    consumer_stat = _lstat(consumer_state, "consumer-state", issues)
    consumer_state_exists = consumer_stat is not None
    if consumer_stat is not None:
        if not _is_directory(consumer_stat):
            issues.append("consumer-state: expected a real directory")
        else:
            has_consumer_state_directory = True
            try:
                consumer_entries = sorted(
                    consumer_state.iterdir(), key=lambda entry: entry.name
                )
            except OSError as exc:
                issues.append(
                    "consumer-state: entries could not be inspected "
                    f"({type(exc).__name__})"
                )

    if consumer_entries:
        try:
            from inbox_consumer import (
                ConsumerIntegrityError,
                _state_filename,
                _state_from_json,
            )
        except ImportError as exc:
            issues.append(
                "consumer-state: existing validator could not be loaded "
                f"({type(exc).__name__})"
            )
        else:
            for entry in consumer_entries:
                entry_stat = _lstat(entry, "consumer-state component", issues)
                if entry_stat is None:
                    continue
                if _TEMPORARY_NAME.fullmatch(entry.name):
                    issues.append(f"consumer-state temporary component: {entry.name}")
                    continue
                if not _CONSUMER_STATE_NAME.fullmatch(entry.name):
                    issues.append(
                        f"unrecognized consumer-state component: {entry.name}"
                    )
                    continue
                if not _is_regular_file(entry_stat):
                    corrupt = True
                    details.append(
                        f"consumer-state component {entry.name}: "
                        "expected a regular non-symlink file"
                    )
                    continue
                try:
                    serialized = entry.read_bytes().decode("utf-8")
                    parsed = json.loads(serialized)
                    ingestion_key = (
                        parsed.get("ingestion_key")
                        if isinstance(parsed, dict)
                        else None
                    )
                    if (
                        not isinstance(ingestion_key, str)
                        or _state_filename(ingestion_key) != entry.name
                    ):
                        raise ValueError("filename does not match ingestion key")
                    state = _state_from_json(serialized, ingestion_key)
                except (
                    ConsumerIntegrityError,
                    OSError,
                    UnicodeError,
                    ValueError,
                    TypeError,
                    RecursionError,
                ) as exc:
                    corrupt = True
                    details.append(
                        f"consumer-state component {entry.name}: invalid "
                        f"({type(exc).__name__})"
                    )
                    continue
                sidecar_counts[state.status.value] += 1
    if consumer_state_exists and consumer_stat is not None and _is_directory(consumer_stat):
        details.append(
            "consumer-state: "
            + ", ".join(
                f"{status}={count}" for status, count in sidecar_counts.items()
            )
        )

    for component, path in (
        ("watcher lock", watcher_lock),
        ("consumer lock", consumer_lock),
    ):
        lock_stat = _lstat(path, component, issues)
        if lock_stat is not None:
            if not _is_regular_file(lock_stat):
                issues.append(f"{component}: expected a regular non-symlink file")
            elif component == "watcher lock" and watcher_lock_owned:
                details.append("watcher lock: held by this bootstrap process")
            elif reject_present_locks:
                issues.append(
                    f"{component}: present; bootstrap requires no existing lock"
                )
            else:
                details.append(
                    f"{component}: present; active ownership is not checked "
                    "in read-only mode"
                )

    rejected_stat = _lstat(rejected_file, "AI failure spool", issues)
    rejected_exists = rejected_stat is not None
    if rejected_stat is not None:
        if not _is_regular_file(rejected_stat):
            issues.append("AI failure spool: expected a regular non-symlink file")
        else:
            details.append(
                f"AI failure spool: present ({rejected_stat.st_size} bytes, "
                "content not read)"
            )

    if checkpoint_value is not None:
        try:
            resolved_source = _retry_source_metadata(
                lambda: source.resolve(strict=True)
            )
            source_stat = _retry_source_metadata(resolved_source.stat)
        except (FileNotFoundError, RuntimeError):
            source_mismatch = True
            details.append("source: file is missing or cannot be resolved")
        except OSError as exc:
            issues.append(
                f"source: metadata could not be inspected ({type(exc).__name__})"
            )
        else:
            if not stat.S_ISREG(source_stat.st_mode):
                source_mismatch = True
                details.append("source: expected a regular file")
            else:
                try:
                    source_identity = _source_identity(source_stat)
                except BootstrapError:
                    source_mismatch = True
                    details.append("source: stable file identity is unavailable")
                    source_identity = None
                try:
                    checkpoint_source = Path(
                        checkpoint_value.source_path
                    )
                    checkpoint_source = _retry_source_metadata(
                        lambda: checkpoint_source.resolve(strict=True)
                    )
                except (OSError, RuntimeError):
                    source_mismatch = True
                    details.append("source: checkpoint source path is unavailable")
                else:
                    if source_identity is not None and (
                        checkpoint_source != resolved_source
                        or checkpoint_value.source_file_identity != source_identity
                        or checkpoint_value.next_byte_offset > source_stat.st_size
                    ):
                        source_mismatch = True
                        details.append(
                            "source: path, file identity, or size does not match "
                            "the checkpoint"
                        )
                    elif source_identity is not None:
                        details.append(
                            f"source: identity matches; size {source_stat.st_size} bytes"
                        )

    has_state_artifacts = bool(
        inbox_exists
        or consumer_state_exists
        or consumer_entries
        or rejected_exists
        or (checkpoint_stat is not None)
    )
    has_persisted_state_without_checkpoint = (
        checkpoint_value is None
        and checkpoint_stat is None
        and (
            inbox_exists
            or consumer_state_exists
            or bool(consumer_entries)
            or rejected_exists
        )
    )

    if corrupt:
        status = PipelineStateStatus.CORRUPT
    elif checkpoint_value is not None and not inbox_exists:
        status = PipelineStateStatus.INCONSISTENT
        details.append("Inbox: missing while a valid checkpoint exists")
    elif issues:
        status = PipelineStateStatus.INCONSISTENT
    elif has_persisted_state_without_checkpoint:
        status = PipelineStateStatus.INCONSISTENT
        details.append("state: persisted components exist without a checkpoint")
    elif source_mismatch:
        status = PipelineStateStatus.SOURCE_MISMATCH
    elif checkpoint_value is None:
        status = PipelineStateStatus.UNINITIALIZED
        if not has_state_artifacts:
            details.append("state: no checkpoint or persisted event state found")
        else:
            details.append("state: no checkpoint; no event-bearing state found")
    elif has_consumer_state_directory:
        status = PipelineStateStatus.BOOTSTRAPPED_V2
        details.append(
            "classification: V2 is inferred from the valid consumer-state "
            "directory; no explicit bootstrap-version marker exists"
        )
    else:
        status = PipelineStateStatus.LEGACY_VALID
        details.append(
            "classification: valid checkpoint and Inbox without consumer-state "
            "directory"
        )

    details.extend(issues)
    return PipelineStateInspection(status=status, details=tuple(details))


def _source_identity(file_stat: os.stat_result) -> str:
    device_id = getattr(file_stat, "st_dev", None)
    file_id = getattr(file_stat, "st_ino", None)
    if (
        type(device_id) is not int
        or type(file_id) is not int
        or device_id < 0
        or file_id <= 0
    ):
        raise BootstrapError("source OS device and file identity are unavailable")
    return f"stat-v1:{device_id}:{file_id}"


def bootstrap_state(
    mode: BootstrapMode,
    *,
    source_path: PathLike = LOG_FILE_PATH,
    checkpoint_path: PathLike = CHECKPOINT_FILE_PATH,
    inbox_directory: PathLike = INBOX_DIRECTORY_PATH,
    lock_path: PathLike = LOCK_FILE_PATH,
    consumer_state_directory: PathLike = CONSUMER_STATE_DIRECTORY_PATH,
    consumer_lock_path: PathLike = CONSUMER_LOCK_FILE_PATH,
    rejected_file_path: PathLike = REJECTED_FILE_PATH,
) -> BootstrapResult:
    """Initialize a missing checkpoint at an explicitly selected file boundary.

    This function never consumes source records. In ``from-end`` mode it reads
    only the final byte to verify that the chosen offset is an LF boundary.
    """
    if mode not in ("from-start", "from-end"):
        raise ValueError("mode must be either 'from-start' or 'from-end'")

    source = Path(source_path).resolve(strict=True)
    checkpoint = Path(checkpoint_path)
    inbox = Path(inbox_directory)
    consumer_state = Path(consumer_state_directory)
    consumer_lock = Path(consumer_lock_path)
    rejected_file = Path(rejected_file_path)
    lock = SingleWriterLock(
        lock_path,
        checkpoint_path=checkpoint,
        inbox_directory=inbox,
    )

    if not checkpoint.parent.is_dir():
        raise BootstrapError(
            "checkpoint parent directory must already exist; state is not created"
        )
    if inbox.parent != checkpoint.parent and not inbox.parent.is_dir():
        raise BootstrapError(
            "Inbox parent directory must already exist; state is not created"
        )

    def require_uninitialized(*, watcher_lock_owned: bool) -> None:
        inspection = inspect_pipeline_state(
            source_path=source,
            checkpoint_path=checkpoint,
            inbox_directory=inbox,
            consumer_state_directory=consumer_state,
            lock_path=lock_path,
            consumer_lock_path=consumer_lock,
            rejected_file_path=rejected_file,
            watcher_lock_owned=watcher_lock_owned,
            reject_present_locks=True,
        )
        if inspection.status is not PipelineStateStatus.UNINITIALIZED:
            details = "; ".join(inspection.details)
            raise BootstrapError(
                "bootstrap requires UNINITIALIZED state; found "
                f"{inspection.status.value}"
                + (f": {details}" if details else "")
            )

    require_uninitialized(watcher_lock_owned=False)

    with lock:
        require_uninitialized(watcher_lock_owned=True)
        with source.open("rb") as source_file:
            file_stat = os.fstat(source_file.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                raise BootstrapError("configured source must be a regular file")
            identity = _source_identity(file_stat)

            if mode == "from-start":
                offset = 0
            else:
                offset = file_stat.st_size

            try:
                source_prefix_sha256, last_byte, _ = hash_source_prefix(
                    source_file,
                    offset,
                )
            except CheckpointSourceError as exc:
                raise SourceIdentityMismatchError(
                    "source ended before the selected bootstrap offset"
                ) from exc
            if offset and last_byte != b"\n":
                raise BootstrapError(
                    "from-end requires the selected prefix to end in LF; "
                    "no checkpoint was created"
                )

            try:
                current_source = source.stat()
                current_file_stat = os.fstat(source_file.fileno())
            except OSError as exc:
                raise SourceIdentityMismatchError(
                    "source path is no longer available"
                ) from exc
            if (
                _source_identity(current_source) != identity
                or _source_identity(current_file_stat) != identity
            ):
                raise SourceIdentityMismatchError(
                    "source file identity changed during bootstrap"
                )
            if (
                current_source.st_size < file_stat.st_size
                or current_file_stat.st_size < file_stat.st_size
                or current_source.st_size < offset
                or current_file_stat.st_size < offset
            ):
                raise SourceIdentityMismatchError(
                    "source file became shorter during bootstrap hashing"
                )

            try:
                inbox.mkdir()
            except FileExistsError as exc:
                raise BootstrapError(
                    "Inbox appeared during bootstrap; it was not modified"
                ) from exc
            except OSError as exc:
                raise BootstrapError("Inbox directory could not be created") from exc

            initial_checkpoint = Checkpoint(
                version=CHECKPOINT_VERSION,
                source_path=str(source),
                source_file_identity=identity,
                next_byte_offset=offset,
                last_ingestion_key=None,
                updated_at=datetime.now(timezone.utc).isoformat(),
                source_prefix_sha256=source_prefix_sha256,
            )
            result = create_checkpoint_if_absent(checkpoint, initial_checkpoint)
            if result.outcome is None:
                raise BootstrapError(
                    "checkpoint writer returned without a durability outcome"
                )
            if not result.outcome.is_durable_success:
                raise DurabilityError(
                    "initial checkpoint was not confirmed durable",
                    operation="checkpoint_write",
                    publication_state=result.outcome.publication_state,
                    directory_sync_state=result.outcome.directory_sync_state,
                    destination=checkpoint,
                )
            return BootstrapResult(initial_checkpoint, created=True)


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect Pipeline state without writing, or explicitly bootstrap "
            "a missing Durable Log Watcher checkpoint."
        )
    )
    parser.add_argument(
        "command",
        choices=("inspect", "from-start", "from-end"),
        help="'inspect' is read-only; bootstrap modes require explicit selection",
    )
    parser.add_argument("--source", default=LOG_FILE_PATH)
    parser.add_argument("--checkpoint", default=CHECKPOINT_FILE_PATH)
    parser.add_argument("--inbox", default=INBOX_DIRECTORY_PATH)
    parser.add_argument("--lock", default=LOCK_FILE_PATH)
    parser.add_argument(
        "--consumer-state", default=CONSUMER_STATE_DIRECTORY_PATH
    )
    parser.add_argument("--consumer-lock", default=CONSUMER_LOCK_FILE_PATH)
    parser.add_argument("--rejected-file", default=REJECTED_FILE_PATH)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    arguments = _build_argument_parser().parse_args(argv)
    if arguments.command == "inspect":
        result = inspect_pipeline_state(
            source_path=arguments.source,
            checkpoint_path=arguments.checkpoint,
            inbox_directory=arguments.inbox,
            consumer_state_directory=arguments.consumer_state,
            lock_path=arguments.lock,
            consumer_lock_path=arguments.consumer_lock,
            rejected_file_path=arguments.rejected_file,
        )
        print(f"Pipeline state: {result.status.value}")
        for detail in result.details:
            print(f"- {detail}")
        return (
            0
            if result.status
            in {
                PipelineStateStatus.UNINITIALIZED,
                PipelineStateStatus.LEGACY_VALID,
                PipelineStateStatus.BOOTSTRAPPED_V2,
            }
            else 1
        )

    try:
        result = bootstrap_state(
            arguments.command,
            source_path=arguments.source,
            checkpoint_path=arguments.checkpoint,
            inbox_directory=arguments.inbox,
            lock_path=arguments.lock,
            consumer_state_directory=arguments.consumer_state,
            consumer_lock_path=arguments.consumer_lock,
            rejected_file_path=arguments.rejected_file,
        )
    except (
        BootstrapError,
        CheckpointError,
        SingleWriterLockError,
        DurabilityError,
        OSError,
        ValueError,
    ) as exc:
        LOGGER.error("Bootstrap failed: %s", exc)
        return 2

    if result.created:
        LOGGER.info(
            "Created initial checkpoint in %s mode at byte offset %d. "
            "Start the watcher normally.",
            arguments.command,
            result.checkpoint.next_byte_offset,
        )
    else:
        LOGGER.info(
            "A valid checkpoint already exists at byte offset %d; "
            "it was not changed. Start the watcher normally.",
            result.checkpoint.next_byte_offset,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
