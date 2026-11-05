"""Durable, checkpointed reader for the configured source log."""

import errno
import hashlib
import logging
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NoReturn, Optional, TypeVar, Union

from checkpoint import (
    CHECKPOINT_VERSION,
    Checkpoint,
    CheckpointError,
    CheckpointNotFoundError,
    CheckpointSourceError,
    hash_source_prefix,
    load_checkpoint,
    write_checkpoint,
)
from bootstrap_state import (
    PipelineStateStatus,
    inspect_pipeline_state,
)
from config import (
    CHECKPOINT_FILE_PATH,
    CONSUMER_LOCK_FILE_PATH,
    CONSUMER_STATE_DIRECTORY_PATH,
    INBOX_DIRECTORY_PATH,
    LOCK_FILE_PATH,
    LOG_FILE_PATH,
    LOG_FORMAT,
    POLLING_INTERVAL,
    REJECTED_FILE_PATH,
)
from inbox_consumer import InboxConsumer
from inbox_record import create_inbox_event
from inbox_writer import write_inbox_event
from single_writer_lock import SingleWriterLock


LOGGER = logging.getLogger(__name__)
PathLike = Union[str, os.PathLike]
_SOURCE_IO_RETRY_DELAYS = (0.25, 0.5, 1.0, 2.0)
_TRANSIENT_SOURCE_ERRNOS = {
    errno.EFAULT,
    errno.EINTR,
    errno.EAGAIN,
    getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
}
_Result = TypeVar("_Result")
_MAX_SOURCE_IO_ATTEMPTS = len(_SOURCE_IO_RETRY_DELAYS) + 1


def _is_transient_source_error(error: OSError) -> bool:
    return error.errno in _TRANSIENT_SOURCE_ERRNOS


def _retry_source_io(
    operation: Callable[[], _Result],
    operation_name: str,
    *,
    budget: Optional["_SourceIORetryBudget"] = None,
) -> _Result:
    retry_budget = budget or _SourceIORetryBudget()
    return retry_budget.run(operation, operation_name)


class _SourceIORetryBudget:
    """Share one bounded attempt budget across nested source operations."""

    def __init__(self) -> None:
        self.attempts = 0
        self.transient_failures = 0
        self.operation_name: Optional[str] = None

    def run(
        self,
        operation: Callable[[], _Result],
        operation_name: str,
    ) -> _Result:
        if self.operation_name is None:
            self.operation_name = operation_name

        while True:
            if self.attempts >= _MAX_SOURCE_IO_ATTEMPTS:
                raise SourceIOError(
                    f"source {self.operation_name} exhausted the shared retry "
                    f"budget after {self.attempts} attempts"
                )

            self.attempts += 1
            try:
                return operation()
            except OSError as exc:
                if not _is_transient_source_error(exc):
                    raise
                self.transient_failures += 1
                if self.attempts >= _MAX_SOURCE_IO_ATTEMPTS:
                    raise SourceIOError(
                        f"source {self.operation_name} failed after "
                        f"{self.attempts} attempts: {exc}"
                    ) from exc
                delay_index = min(
                    self.transient_failures - 1,
                    len(_SOURCE_IO_RETRY_DELAYS) - 1,
                )
                time.sleep(_SOURCE_IO_RETRY_DELAYS[delay_index])


class DurableWatcherError(RuntimeError):
    """Base error for failures that prevent safe durable log ingestion."""


class BootstrapRequiredError(DurableWatcherError):
    """Raised when an explicit initial checkpoint has not been prepared."""


class SourceIdentityMismatchError(DurableWatcherError):
    """Raised when the source file cannot be verified against its checkpoint."""


class SourceIOError(DurableWatcherError):
    """Raised when transient source I/O errors persist after bounded retries."""


class DurableLogWatcher:
    """Read complete source records and persist Inbox before advancing state."""

    def __init__(
        self,
        source_path: PathLike = LOG_FILE_PATH,
        checkpoint_path: PathLike = CHECKPOINT_FILE_PATH,
        inbox_directory: PathLike = INBOX_DIRECTORY_PATH,
        lock_path: PathLike = LOCK_FILE_PATH,
        polling_interval: float = POLLING_INTERVAL,
        consumer_state_directory: PathLike = CONSUMER_STATE_DIRECTORY_PATH,
        consumer_lock_path: PathLike = CONSUMER_LOCK_FILE_PATH,
        rejected_file_path: PathLike = REJECTED_FILE_PATH,
    ) -> None:
        self.source_path = Path(source_path)
        self.checkpoint_path = Path(checkpoint_path)
        self.inbox_directory = Path(inbox_directory)
        self.lock_path = Path(lock_path)
        self.consumer_state_directory = Path(consumer_state_directory)
        self.consumer_lock_path = Path(consumer_lock_path)
        self.rejected_file_path = Path(rejected_file_path)
        self.polling_interval = polling_interval
        self._lock = SingleWriterLock(
            self.lock_path,
            checkpoint_path=self.checkpoint_path,
            inbox_directory=self.inbox_directory,
        )
        self._source_file = None
        self._source_path_resolved: Optional[Path] = None
        self._source_file_identity: Optional[str] = None
        self._next_byte_offset: Optional[int] = None
        self._source_hasher = None

    def __enter__(self) -> "DurableLogWatcher":
        self._preflight_startup()

        self._lock.__enter__()
        try:
            self._preflight_startup(watcher_lock_owned=True)
            self._source_path_resolved = self.source_path.resolve(strict=True)
            self._source_file = self._source_path_resolved.open("rb")
            checkpoint = self._load_existing_checkpoint()
            self._initialize_from_checkpoint(checkpoint)
        except BaseException:
            self._close_source_file()
            self._lock.release()
            raise
        return self

    def _preflight_startup(self, *, watcher_lock_owned: bool = False) -> None:
        try:
            def inspect_source_metadata():
                resolved = self.source_path.resolve(strict=True)
                return resolved, resolved.stat()

            resolved_source, source_stat = _retry_source_io(
                inspect_source_metadata,
                "startup source metadata inspection",
            )
        except SourceIOError:
            raise
        except (OSError, RuntimeError) as exc:
            message = f"source log is missing or inaccessible: {self.source_path}"
            LOGGER.error("Watcher startup preflight failed: %s", message)
            raise DurableWatcherError(message) from exc

        if not stat.S_ISREG(source_stat.st_mode):
            self._raise_preflight_error(
                f"source log must be a regular file: {resolved_source}"
            )

        inspection = inspect_pipeline_state(
            source_path=resolved_source,
            checkpoint_path=self.checkpoint_path,
            inbox_directory=self.inbox_directory,
            consumer_state_directory=self.consumer_state_directory,
            lock_path=self.lock_path,
            consumer_lock_path=self.consumer_lock_path,
            rejected_file_path=self.rejected_file_path,
            watcher_lock_owned=watcher_lock_owned,
        )
        if not self.checkpoint_path.exists():
            if inspection.status is PipelineStateStatus.UNINITIALIZED:
                message = (
                    "Pipeline state is UNINITIALIZED; the watcher will not "
                    "bootstrap automatically. After reviewing the source and "
                    "state, run `python 2_log_pipeline/bootstrap_state.py "
                    "from-start` or `python 2_log_pipeline/bootstrap_state.py "
                    "from-end` explicitly."
                )
            else:
                message = (
                    "Checkpoint is missing and Pipeline state is "
                    f"{inspection.status.value}; the watcher will not "
                    "bootstrap automatically. Review the existing state before "
                    "choosing an explicit bootstrap mode. "
                    + "; ".join(inspection.details)
                )
            self._raise_preflight_error(
                message,
                bootstrap_required=True,
            )

        if inspection.status is PipelineStateStatus.SOURCE_MISMATCH:
            self._raise_preflight_error(
                "Pipeline startup preflight rejected state "
                f"{inspection.status.value}: {'; '.join(inspection.details)}",
                source_mismatch=True,
            )
        if inspection.status not in {
            PipelineStateStatus.LEGACY_VALID,
            PipelineStateStatus.BOOTSTRAPPED_V2,
        }:
            self._raise_preflight_error(
                "Pipeline startup preflight rejected state "
                f"{inspection.status.value}: {'; '.join(inspection.details)}"
            )

        try:
            checkpoint = load_checkpoint(self.checkpoint_path)
            with resolved_source.open("rb") as source_file:
                self._validate_checkpoint_source(
                    checkpoint,
                    resolved_source,
                    source_file,
                )
        except CheckpointNotFoundError:
            self._raise_preflight_error(
                "Checkpoint disappeared during startup preflight; "
                "the watcher did not bootstrap automatically."
            )
        except CheckpointError as exc:
            self._raise_preflight_error(
                f"Checkpoint validation failed during startup preflight: {exc}"
            )
        except OSError as exc:
            self._raise_preflight_error(
                f"Source/checkpoint could not be validated: {exc}"
            )
        except SourceIdentityMismatchError as exc:
            LOGGER.error("Watcher startup preflight failed: %s", exc)
            raise
        except RuntimeError as exc:
            self._raise_preflight_error(
                f"Source/checkpoint path could not be validated: {exc}"
            )

    @staticmethod
    def _raise_preflight_error(
        message: str,
        *,
        bootstrap_required: bool = False,
        source_mismatch: bool = False,
    ) -> NoReturn:
        LOGGER.error("Watcher startup preflight failed: %s", message)
        if bootstrap_required:
            raise BootstrapRequiredError(message)
        if source_mismatch:
            raise SourceIdentityMismatchError(message)
        raise DurableWatcherError(message)

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            self._close_source_file()
        finally:
            self._lock.__exit__(exc_type, exc_value, traceback)
        return False

    @property
    def next_byte_offset(self) -> int:
        if self._next_byte_offset is None:
            raise DurableWatcherError("watcher has not been initialized")
        return self._next_byte_offset

    def poll_once(self) -> int:
        """Ingest all currently complete records and return their count."""
        self._require_open_source()
        ingested = 0

        while True:
            record_start = self.next_byte_offset
            raw_record_bytes = self._read_source_line(record_start)
            if not raw_record_bytes or not raw_record_bytes.endswith(b"\n"):
                self._seek_source(record_start)
                return ingested

            record_end = record_start + len(raw_record_bytes)
            self._verify_source_identity()
            if self._source_hasher is None:
                raise DurableWatcherError("source prefix hasher is not initialized")
            candidate_hasher = self._source_hasher.copy()
            candidate_hasher.update(raw_record_bytes)
            inbox_event = create_inbox_event(
                self._require_source_identity(),
                record_start,
                record_end,
                raw_record_bytes,
                source_format=LOG_FORMAT,
            )
            result = write_inbox_event(self.inbox_directory, inbox_event)
            if not result.outcome.is_durable_success:
                raise DurableWatcherError(
                    "Inbox writer returned without DURABLE/SYNCED confirmation"
                )

            checkpoint = Checkpoint(
                version=CHECKPOINT_VERSION,
                source_path=str(self._require_resolved_source_path()),
                source_file_identity=self._require_source_identity(),
                next_byte_offset=record_end,
                last_ingestion_key=inbox_event.ingestion_key,
                updated_at=datetime.now(timezone.utc).isoformat(),
                source_prefix_sha256=candidate_hasher.hexdigest(),
            )
            checkpoint_result = write_checkpoint(
                self.checkpoint_path,
                checkpoint,
                create_directories=False,
            )
            if (
                checkpoint_result.outcome is None
                or not checkpoint_result.outcome.is_durable_success
            ):
                raise DurableWatcherError(
                    "Checkpoint writer returned without DURABLE/SYNCED confirmation"
                )

            self._next_byte_offset = record_end
            self._source_hasher = candidate_hasher
            ingested += 1
            LOGGER.debug(
                "Durably ingested source range [%d, %d) as %s",
                record_start,
                record_end,
                inbox_event.ingestion_key,
            )

    def run_forever(self) -> None:
        """Capture source records and consume Inbox while holding watcher lock."""
        with self:
            consumer = InboxConsumer(
                inbox_directory=self.inbox_directory,
                checkpoint_path=self.checkpoint_path,
            )
            LOGGER.info(
                "Watching %s from byte offset %d",
                self._require_resolved_source_path(),
                self.next_byte_offset,
            )
            while True:
                self.poll_once()
                consumer.consume_once()
                time.sleep(self.polling_interval)

    def _load_existing_checkpoint(self) -> Checkpoint:
        try:
            return load_checkpoint(self.checkpoint_path)
        except CheckpointNotFoundError as exc:
            raise BootstrapRequiredError(
                "checkpoint is missing; prepare an explicit initial checkpoint "
                "before starting the durable watcher"
            ) from exc

    def _initialize_from_checkpoint(self, checkpoint: Checkpoint) -> None:
        source_file = self._require_open_source()
        resolved_source = self._require_resolved_source_path()
        actual_identity = self._validate_checkpoint_source(
            checkpoint,
            resolved_source,
            source_file,
        )
        self._source_file_identity = actual_identity
        self._next_byte_offset = checkpoint.next_byte_offset
        self._source_hasher = self._hash_checkpoint_prefix(checkpoint, source_file)

        self._seek_source(self.next_byte_offset)

    def _hash_checkpoint_prefix(self, checkpoint: Checkpoint, source_file):
        source_path = self._require_resolved_source_path()
        source_identity = checkpoint.source_file_identity
        prefix_offset = checkpoint.next_byte_offset

        def verify_snapshot() -> int:
            descriptor_stat = os.fstat(source_file.fileno())
            try:
                configured_path = self.source_path.resolve(strict=True)
                path_stat = configured_path.stat()
            except OSError as exc:
                if _is_transient_source_error(exc):
                    raise
                raise SourceIdentityMismatchError(
                    "source path is unavailable during checkpoint hash verification"
                ) from exc

            if configured_path != source_path:
                raise SourceIdentityMismatchError(
                    "configured source path changed during checkpoint hash verification"
                )
            if (
                not stat.S_ISREG(descriptor_stat.st_mode)
                or not stat.S_ISREG(path_stat.st_mode)
            ):
                raise SourceIdentityMismatchError(
                    "source must remain a regular file during checkpoint hash verification"
                )
            if (
                source_file_identity_from_stat(descriptor_stat) != source_identity
                or source_file_identity_from_stat(path_stat) != source_identity
            ):
                raise SourceIdentityMismatchError(
                    "source identity changed during checkpoint hash verification"
                )
            if descriptor_stat.st_size < prefix_offset or path_stat.st_size < prefix_offset:
                raise SourceIdentityMismatchError(
                    "source became shorter than the checkpoint offset during hashing"
                )
            return descriptor_stat.st_size

        initial_size = _retry_source_io(
            verify_snapshot,
            "checkpoint hash preflight",
        )

        def calculate_prefix():
            digest, last_byte, candidate_hasher = hash_source_prefix(
                source_file,
                prefix_offset,
            )
            return digest, last_byte, candidate_hasher

        try:
            digest, last_byte, candidate_hasher = _retry_source_io(
                calculate_prefix,
                "checkpoint source prefix hash",
            )
        except CheckpointSourceError as exc:
            raise SourceIdentityMismatchError(str(exc)) from exc

        final_size = _retry_source_io(
            verify_snapshot,
            "checkpoint hash postflight",
        )
        if final_size < initial_size:
            raise SourceIdentityMismatchError(
                "source size decreased while hashing the committed prefix"
            )
        if prefix_offset > 0 and last_byte != b"\n":
            raise SourceIdentityMismatchError(
                "checkpoint offset is not immediately after a complete LF record"
            )
        if digest != checkpoint.source_prefix_sha256:
            raise SourceIdentityMismatchError(
                "source prefix SHA-256 does not match the checkpoint"
            )
        return candidate_hasher

    @staticmethod
    def _validate_checkpoint_source(
        checkpoint: Checkpoint,
        resolved_source: Path,
        source_file,
    ) -> str:
        def validate() -> str:
            if os.path.normcase(os.fspath(Path(checkpoint.source_path).resolve())) != (
                os.path.normcase(os.fspath(resolved_source))
            ):
                raise SourceIdentityMismatchError(
                    "checkpoint source_path does not match the configured source path"
                )

            source_stat = os.fstat(source_file.fileno())
            actual_identity = source_file_identity_from_stat(source_stat)
            if actual_identity != checkpoint.source_file_identity:
                raise SourceIdentityMismatchError(
                    "source file identity does not match the checkpoint generation"
                )
            if not stat.S_ISREG(source_stat.st_mode):
                raise SourceIdentityMismatchError("source must be a regular file")
            if checkpoint.next_byte_offset > source_stat.st_size:
                raise SourceIdentityMismatchError(
                    "source file is shorter than its committed checkpoint offset"
                )
            if checkpoint.next_byte_offset > 0:
                source_file.seek(checkpoint.next_byte_offset - 1)
                if source_file.read(1) != b"\n":
                    raise SourceIdentityMismatchError(
                        "checkpoint offset is not immediately after a complete LF record"
                    )
            return actual_identity

        return _retry_source_io(validate, "checkpoint source validation")

    def _verify_source_identity(
        self,
        retry_budget: Optional[_SourceIORetryBudget] = None,
    ) -> None:
        source_file = self._require_open_source()
        source_path = self._require_resolved_source_path()

        def verify() -> None:
            descriptor_stat = os.fstat(source_file.fileno())
            try:
                configured_path = self.source_path.resolve(strict=True)
                path_stat = source_path.stat()
            except OSError as exc:
                if _is_transient_source_error(exc):
                    raise
                raise SourceIdentityMismatchError(
                    "configured source path is no longer available"
                ) from exc
            if configured_path != source_path:
                raise SourceIdentityMismatchError(
                    "configured source path now resolves to a different file"
                )
            if not stat.S_ISREG(descriptor_stat.st_mode) or not stat.S_ISREG(
                path_stat.st_mode
            ):
                raise SourceIdentityMismatchError("source must remain a regular file")

            expected_identity = self._require_source_identity()
            if (
                source_file_identity_from_stat(descriptor_stat) != expected_identity
                or source_file_identity_from_stat(path_stat) != expected_identity
            ):
                raise SourceIdentityMismatchError(
                    "source file identity changed; rotation is not handled automatically"
                )
            if self.next_byte_offset > descriptor_stat.st_size:
                raise SourceIdentityMismatchError(
                    "source file is shorter than its committed checkpoint offset"
                )

        _retry_source_io(
            verify,
            "identity verification",
            budget=retry_budget,
        )

    def _seek_source(
        self,
        byte_offset: int,
        retry_budget: Optional[_SourceIORetryBudget] = None,
    ) -> None:
        source_file = self._require_open_source()
        budget = retry_budget or _SourceIORetryBudget()

        def seek() -> None:
            self._verify_source_identity(budget)
            source_file.seek(byte_offset)

        _retry_source_io(seek, "seek", budget=budget)

    def _read_source_line(
        self,
        byte_offset: int,
        retry_budget: Optional[_SourceIORetryBudget] = None,
    ) -> bytes:
        source_file = self._require_open_source()
        budget = retry_budget or _SourceIORetryBudget()

        def read_line() -> bytes:
            self._verify_source_identity(budget)
            source_file.seek(byte_offset)
            return source_file.readline()

        return _retry_source_io(read_line, "readline", budget=budget)

    def _require_open_source(self):
        if self._source_file is None or self._source_file.closed:
            raise DurableWatcherError("source file is not open")
        return self._source_file

    def _require_resolved_source_path(self) -> Path:
        if self._source_path_resolved is None:
            raise DurableWatcherError("source path has not been initialized")
        return self._source_path_resolved

    def _require_source_identity(self) -> str:
        if self._source_file_identity is None:
            raise DurableWatcherError("source identity has not been initialized")
        return self._source_file_identity

    def _close_source_file(self) -> None:
        if self._source_file is not None:
            self._source_file.close()
            self._source_file = None


def source_file_identity_from_stat(file_stat: os.stat_result) -> str:
    """Encode the OS device and file ID used by an explicit checkpoint."""
    device_id = getattr(file_stat, "st_dev", None)
    file_id = getattr(file_stat, "st_ino", None)
    if (
        type(device_id) is not int
        or type(file_id) is not int
        or device_id < 0
        or file_id <= 0
    ):
        raise SourceIdentityMismatchError(
            "OS file identity (device and file ID) is unavailable"
        )
    return f"stat-v1:{device_id}:{file_id}"


def start_watching() -> None:
    DurableLogWatcher().run_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    start_watching()
