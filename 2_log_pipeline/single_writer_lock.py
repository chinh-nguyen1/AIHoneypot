"""Cross-platform exclusive lock for durable-ingestion state operations."""

import errno
import os
import stat
import threading
from pathlib import Path
from typing import Optional, Union


PathLike = Union[str, os.PathLike]

_process_lock_guard = threading.Lock()
_process_locked_paths: set[str] = set()


class SingleWriterLockError(RuntimeError):
    """Base error for single-writer lock operations."""


class InvalidLockPathError(SingleWriterLockError, ValueError):
    """Raised when the lock path is invalid or conflicts with protected state."""


class LockContentionError(SingleWriterLockError):
    """Raised when another process or thread already owns this lock path."""


class LockAcquisitionError(SingleWriterLockError):
    """Raised when the OS lock could not be acquired for a non-contention reason."""

    def __init__(self, message: str, original_cause: BaseException) -> None:
        super().__init__(message)
        self.original_cause = original_cause


class LockReleaseError(SingleWriterLockError):
    """Raised when the OS lock or its descriptor could not be released cleanly."""

    def __init__(
        self,
        message: str,
        original_cause: BaseException,
        secondary_cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.original_cause = original_cause
        self.secondary_cause = secondary_cause


class UnsupportedLockingError(SingleWriterLockError):
    """Raised when this platform has no supported locking primitive."""


def _normalized_path(value: PathLike, label: str) -> Path:
    try:
        raw_path = os.fspath(value)
    except TypeError as exc:
        raise InvalidLockPathError(f"{label} must be a filesystem path") from exc
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise InvalidLockPathError(f"{label} must be a non-empty filesystem path")
    try:
        return Path(raw_path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise InvalidLockPathError(f"{label} could not be normalized") from exc


def _path_key(path: Path) -> str:
    return os.path.normcase(os.fspath(path))


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _validate_lock_path(
    lock_path: PathLike,
    checkpoint_path: PathLike,
    inbox_directory: PathLike,
) -> tuple[Path, Path, Path]:
    lock = _normalized_path(lock_path, "lock_path")
    checkpoint = _normalized_path(checkpoint_path, "checkpoint_path")
    inbox = _normalized_path(inbox_directory, "inbox_directory")

    if _path_key(lock) == _path_key(checkpoint):
        raise InvalidLockPathError("lock_path must not equal checkpoint_path")
    if _is_within(lock, inbox):
        raise InvalidLockPathError("lock_path must not be inside inbox_directory")
    if _path_key(lock) == _path_key(inbox):
        raise InvalidLockPathError("lock_path must not equal inbox_directory")
    return lock, checkpoint, inbox


def _open_lock_file(path: Path) -> int:
    if not path.parent.is_dir():
        raise InvalidLockPathError(
            "lock parent directory must already exist; it is not created automatically"
        )
    try:
        existing_mode = path.lstat().st_mode
    except FileNotFoundError:
        existing_mode = None
    except OSError:
        raise

    if existing_mode is not None and (
        stat.S_ISLNK(existing_mode) or not stat.S_ISREG(existing_mode)
    ):
        raise InvalidLockPathError("lock_path must be a regular non-symlink file")

    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0)
    if getattr(os, "O_NOFOLLOW", 0):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise InvalidLockPathError("lock_path must identify a regular file")
        if os.fstat(descriptor).st_size == 0:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.write(descriptor, b"\0") != 1:
                raise OSError("failed to initialize lock byte")
        return descriptor
    except BaseException as original_error:
        try:
            os.close(descriptor)
        except OSError as close_error:
            raise LockAcquisitionError(
                f"could not close lock file after opening: {path}",
                close_error,
            ) from original_error
        raise


def _lock_file(descriptor: int) -> None:
    if os.name == "posix":
        try:
            import fcntl
        except ImportError as exc:
            raise UnsupportedLockingError(
                "fcntl locking is unavailable on this POSIX platform"
            ) from exc

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    if os.name == "nt":
        try:
            import msvcrt
        except ImportError as exc:
            raise UnsupportedLockingError(
                "msvcrt locking is unavailable on this Windows platform"
            ) from exc

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        return
    raise UnsupportedLockingError(f"OS locking is unsupported on {os.name!r}")


def _unlock_file(descriptor: int) -> None:
    if os.name == "posix":
        try:
            import fcntl
        except ImportError as exc:
            raise UnsupportedLockingError(
                "fcntl locking is unavailable on this POSIX platform"
            ) from exc

        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return
    if os.name == "nt":
        try:
            import msvcrt
        except ImportError as exc:
            raise UnsupportedLockingError(
                "msvcrt locking is unavailable on this Windows platform"
            ) from exc

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    raise UnsupportedLockingError(f"OS locking is unsupported on {os.name!r}")


def _is_contention(error: OSError) -> bool:
    contention_errnos = {
        errno.EACCES,
        errno.EAGAIN,
        getattr(errno, "EDEADLK", errno.EAGAIN),
        getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
    }
    if error.errno in contention_errnos:
        return True
    return getattr(error, "winerror", None) in {33, 36}


class SingleWriterLock:
    """Non-blocking process-exclusive lock held for this object's context.

    The checkpoint path and Inbox directory are required so the lock cannot
    alias a checkpoint file or be created alongside Inbox event files.
    Parent directories must already exist. The lock file is persistent and
    contains no ownership metadata; the OS lock, not file existence or PID,
    determines ownership.
    """

    def __init__(
        self,
        lock_path: PathLike,
        *,
        checkpoint_path: PathLike,
        inbox_directory: PathLike,
    ) -> None:
        self.path, self._checkpoint_path, self._inbox_directory = (
            _validate_lock_path(lock_path, checkpoint_path, inbox_directory)
        )
        self._key = _path_key(self.path)
        self._descriptor: Optional[int] = None
        self._entered = False

    def __enter__(self) -> "SingleWriterLock":
        if self._entered:
            raise LockContentionError("this lock instance is already entered")

        with _process_lock_guard:
            if self._key in _process_locked_paths:
                raise LockContentionError(
                    f"lock is already held in this process: {self.path}"
                )
            _process_locked_paths.add(self._key)

        descriptor: Optional[int] = None
        try:
            descriptor = _open_lock_file(self.path)
            _lock_file(descriptor)
        except BaseException as exc:
            close_error: Optional[OSError] = None
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError as cleanup_error:
                    close_error = cleanup_error
            self._discard_process_reservation()
            if close_error is not None:
                raise LockAcquisitionError(
                    f"could not clean up failed lock acquisition: {self.path}",
                    close_error,
                ) from exc
            if isinstance(exc, (InvalidLockPathError, UnsupportedLockingError)):
                raise
            if not isinstance(exc, OSError):
                raise
            if _is_contention(exc):
                raise LockContentionError(
                    f"another process holds the lock: {self.path}"
                ) from exc
            raise LockAcquisitionError(
                f"could not acquire lock: {self.path}",
                exc,
            ) from exc

        self._descriptor = descriptor
        self._entered = True
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        del exc_type, traceback
        try:
            self.release()
        except LockReleaseError as release_error:
            if exc_value is not None:
                raise release_error from exc_value
            raise
        return False

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            raise LockReleaseError(
                "lock is not currently held",
                RuntimeError("no lock descriptor is owned"),
            )

        self._descriptor = None
        unlock_error: Optional[BaseException] = None
        close_error: Optional[BaseException] = None
        try:
            _unlock_file(descriptor)
        except BaseException as exc:
            unlock_error = exc
        try:
            os.close(descriptor)
        except OSError as exc:
            close_error = exc
        finally:
            self._entered = False
            self._discard_process_reservation()

        if unlock_error is not None or close_error is not None:
            primary_error = unlock_error or close_error
            secondary_error = (
                close_error if unlock_error is not None else None
            )
            raise LockReleaseError(
                f"could not release lock cleanly: {self.path}",
                primary_error,
                secondary_error,
            ) from primary_error

    def _discard_process_reservation(self) -> None:
        with _process_lock_guard:
            _process_locked_paths.discard(self._key)
