import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


PIPELINE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE_DIR))

import single_writer_lock
from single_writer_lock import (
    InvalidLockPathError,
    LockAcquisitionError,
    LockContentionError,
    LockReleaseError,
    SingleWriterLock,
    UnsupportedLockingError,
)


_HOLDER_SCRIPT = """
import sys
import time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from single_writer_lock import SingleWriterLock
with SingleWriterLock(
    sys.argv[2], checkpoint_path=sys.argv[3], inbox_directory=sys.argv[4]
):
    Path(sys.argv[5]).write_text("ready", encoding="utf-8")
    while not Path(sys.argv[6]).exists():
        time.sleep(0.01)
"""

_TRY_LOCK_SCRIPT = """
import sys
sys.path.insert(0, sys.argv[1])
from single_writer_lock import LockContentionError, SingleWriterLock
try:
    with SingleWriterLock(
        sys.argv[2], checkpoint_path=sys.argv[3], inbox_directory=sys.argv[4]
    ):
        print("ACQUIRED", flush=True)
except LockContentionError:
    print("CONTENDED", flush=True)
"""


class SingleWriterLockTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.state_directory = self.root / "state"
        self.state_directory.mkdir()
        self.inbox_directory = self.state_directory / "inbox"
        self.inbox_directory.mkdir()
        self.checkpoint_path = self.state_directory / "checkpoint.json"
        self.lock_path = self.state_directory / ".single-writer.lock"

    def make_lock(self, lock_path=None):
        return SingleWriterLock(
            self.lock_path if lock_path is None else lock_path,
            checkpoint_path=self.checkpoint_path,
            inbox_directory=self.inbox_directory,
        )

    def start_holder(self):
        ready_path = self.root / "holder-ready"
        release_path = self.root / "holder-release"
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _HOLDER_SCRIPT,
                str(PIPELINE_DIR),
                str(self.lock_path),
                str(self.checkpoint_path),
                str(self.inbox_directory),
                str(ready_path),
                str(release_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if ready_path.exists():
                return process, release_path
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(
                    f"holder exited before acquiring lock: "
                    f"stdout={stdout!r}, stderr={stderr!r}"
                )
            time.sleep(0.01)

        process.terminate()
        process.communicate(timeout=5)
        self.fail("holder subprocess did not acquire the lock in time")

    def run_lock_attempt(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                _TRY_LOCK_SCRIPT,
                str(PIPELINE_DIR),
                str(self.lock_path),
                str(self.checkpoint_path),
                str(self.inbox_directory),
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout.strip()

    def stop_holder(self, process, release_path):
        release_path.touch()
        stdout, stderr = process.communicate(timeout=8)
        self.assertEqual(process.returncode, 0, f"stdout={stdout!r}, stderr={stderr!r}")

    def test_process_a_acquires_lock(self):
        process, release_path = self.start_holder()
        self.assertIsNone(process.poll())
        self.stop_holder(process, release_path)

    def test_process_b_contends_then_acquires_after_release(self):
        process, release_path = self.start_holder()
        self.assertEqual(self.run_lock_attempt(), "CONTENDED")

        self.stop_holder(process, release_path)
        self.assertEqual(self.run_lock_attempt(), "ACQUIRED")

    def test_process_b_acquires_after_process_a_terminates_abnormally(self):
        process, _ = self.start_holder()
        process.terminate()
        process.communicate(timeout=8)

        self.assertEqual(self.run_lock_attempt(), "ACQUIRED")

    def test_existing_lock_file_does_not_prevent_reacquisition(self):
        with self.make_lock():
            pass

        self.assertTrue(self.lock_path.is_file())
        with self.make_lock():
            self.assertTrue(self.lock_path.is_file())
        self.assertTrue(self.lock_path.is_file())

    def test_two_threads_in_one_process_cannot_acquire_independently(self):
        thread_result = []
        attempted = threading.Event()

        def acquire_from_thread():
            try:
                with self.make_lock():
                    thread_result.append("acquired")
            except LockContentionError:
                thread_result.append("contended")
            finally:
                attempted.set()

        with self.make_lock():
            thread = threading.Thread(target=acquire_from_thread)
            thread.start()
            self.assertTrue(attempted.wait(timeout=5))
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

        self.assertEqual(thread_result, ["contended"])

    def test_lock_is_released_when_context_body_raises(self):
        with self.assertRaisesRegex(RuntimeError, "body failed"):
            with self.make_lock():
                raise RuntimeError("body failed")

        with self.make_lock():
            pass

    def test_invalid_and_conflicting_paths_are_rejected(self):
        for invalid_path in ("", self.checkpoint_path, self.inbox_directory / "event"):
            with self.subTest(path=invalid_path):
                with self.assertRaises(InvalidLockPathError):
                    self.make_lock(invalid_path)

        missing_parent_lock = self.state_directory / "missing" / "lock"
        with self.assertRaises(InvalidLockPathError):
            with self.make_lock(missing_parent_lock):
                self.fail("invalid lock parent must not be created")

        self.assertFalse(missing_parent_lock.parent.exists())

    def test_acquisition_failure_is_distinct_and_cleans_thread_reservation(self):
        cause = PermissionError("open denied")
        with mock.patch.object(
            single_writer_lock,
            "_open_lock_file",
            side_effect=cause,
        ):
            with self.assertRaises(LockAcquisitionError) as raised:
                with self.make_lock():
                    self.fail("acquisition failure must not enter context")

        self.assertIs(raised.exception.original_cause, cause)
        with self.make_lock():
            pass

    def test_unsupported_locking_fails_closed_and_cleans_reservation(self):
        with mock.patch.object(
            single_writer_lock,
            "_lock_file",
            side_effect=UnsupportedLockingError("unsupported"),
        ):
            with self.assertRaises(UnsupportedLockingError):
                with self.make_lock():
                    self.fail("unsupported locking must not enter context")

        with self.make_lock():
            pass

    def test_release_failure_is_reported_and_descriptor_close_releases_lock(self):
        lock = self.make_lock()
        lock.__enter__()
        with mock.patch.object(
            single_writer_lock,
            "_unlock_file",
            side_effect=OSError("unlock failed"),
        ):
            with self.assertRaises(LockReleaseError) as raised:
                lock.release()

        self.assertEqual(str(raised.exception.original_cause), "unlock failed")
        self.assertTrue(self.lock_path.exists())
        with self.make_lock():
            pass

    def test_native_platform_lock_primitive_is_selected(self):
        if os.name == "posix":
            import fcntl

            self.assertTrue(callable(fcntl.flock))
        elif os.name == "nt":
            import msvcrt

            self.assertTrue(callable(msvcrt.locking))
        else:
            self.fail(f"unexpected test platform: {os.name}")


if __name__ == "__main__":
    unittest.main()
