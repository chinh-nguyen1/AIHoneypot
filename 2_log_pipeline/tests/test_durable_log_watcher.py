import base64
import errno
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


PIPELINE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE_DIR))

if "mysql.connector" not in sys.modules:
    import types

    mysql_module = types.ModuleType("mysql")
    mysql_module.__path__ = []
    connector_module = types.ModuleType("mysql.connector")
    connector_module.connect = mock.Mock()
    mysql_module.connector = connector_module
    sys.modules["mysql"] = mysql_module
    sys.modules["mysql.connector"] = connector_module

import checkpoint as checkpoint_module
import inbox_writer as inbox_writer_module
import log_watcher
from checkpoint import (
    CHECKPOINT_VERSION,
    Checkpoint,
    checkpoint_to_dict,
    load_checkpoint,
    write_checkpoint,
)
from durability_contract import (
    DirectorySyncState,
    DurabilityError,
    PublicationState,
)
from inbox_record import ParseState, create_inbox_event, inbox_event_from_json
from log_watcher import (
    BootstrapRequiredError,
    DurableLogWatcher,
    DurableWatcherError,
    SourceIdentityMismatchError,
    SourceIOError,
    source_file_identity_from_stat,
)


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


class DurableLogWatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.source_directory = self.root / "logs"
        self.source_directory.mkdir()
        self.state_directory = self.root / "state"
        self.state_directory.mkdir()
        self.inbox_directory = self.state_directory / "inbox"
        self.inbox_directory.mkdir()
        self.source_path = self.source_directory / "access.log"
        self.source_path.write_bytes(b"")
        self.checkpoint_path = self.state_directory / "checkpoint.json"
        self.lock_path = self.state_directory / "single-writer.lock"

    @staticmethod
    def record(event_id="0123456789abcdef0123456789abcdef"):
        data = {
            "source_event_id": event_id,
            "timestamp": "2026-10-31T12:34:56+00:00",
            "source_ip": "192.0.2.10",
            "method": "POST",
            "uri": "/comment.php",
            "query_params": {"q": "áo dài"},
            "body_params": {"content": "raw"},
            "payload": {
                "query_params": {"q": "áo dài"},
                "body_params": {"content": "raw"},
            },
            "user_agent": "unit-test",
            "request_type": "web",
        }
        return (json.dumps(data, ensure_ascii=False) + "\n").encode("utf-8")

    def make_watcher(self, polling_interval=0.01):
        return DurableLogWatcher(
            source_path=self.source_path,
            checkpoint_path=self.checkpoint_path,
            inbox_directory=self.inbox_directory,
            lock_path=self.lock_path,
            consumer_state_directory=self.state_directory / "consumer",
            consumer_lock_path=self.state_directory / "consumer.lock",
            rejected_file_path=self.state_directory / "rejected.jsonl",
            polling_interval=polling_interval,
        )

    def state_snapshot(self):
        return {
            path.relative_to(self.state_directory).as_posix(): (
                "directory" if path.is_dir() else path.read_bytes()
            )
            for path in self.state_directory.rglob("*")
        }

    def prepare_checkpoint(self, next_byte_offset=0, last_ingestion_key=None):
        identity = source_file_identity_from_stat(self.source_path.stat())
        checkpoint = Checkpoint(
            version=CHECKPOINT_VERSION,
            source_path=str(self.source_path.resolve()),
            source_file_identity=identity,
            next_byte_offset=next_byte_offset,
            last_ingestion_key=last_ingestion_key,
            updated_at=datetime.now(timezone.utc).isoformat(),
            source_prefix_sha256=hashlib.sha256(
                self.source_path.read_bytes()[:next_byte_offset]
            ).hexdigest(),
        )
        with mock.patch.object(checkpoint_module, "_fsync_directory"):
            write_checkpoint(self.checkpoint_path, checkpoint)
        return checkpoint

    def enable_test_directory_sync(self):
        self.enterContext(
            mock.patch.object(inbox_writer_module, "_fsync_directory")
        )
        self.enterContext(
            mock.patch.object(checkpoint_module, "_fsync_directory")
        )

    def read_inbox_events(self):
        return [
            inbox_event_from_json(path.read_text(encoding="utf-8"))
            for path in self.inbox_directory.glob("*.event.json")
        ]

    class ReadFailureProxy:
        def __init__(self, source_file, persistent=False, fail_seek_once=False):
            self.source_file = source_file
            self.persistent = persistent
            self.fail_seek_once = fail_seek_once
            self.failed = False
            self.readline_attempts = 0
            self.seek_attempts = 0

        @property
        def closed(self):
            return self.source_file.closed

        def fileno(self):
            return self.source_file.fileno()

        def seek(self, byte_offset):
            self.seek_attempts += 1
            if self.fail_seek_once and self.seek_attempts == 1:
                raise OSError(errno.EFAULT, "simulated seek failure")
            return self.source_file.seek(byte_offset)

        def readline(self):
            self.readline_attempts += 1
            if self.persistent or not self.failed:
                self.failed = True
                self.source_file.read(5)
                raise OSError(errno.EFAULT, "simulated read failure")
            return self.source_file.readline()

        def close(self):
            self.source_file.close()

    def test_missing_checkpoint_fails_closed(self):
        self.inbox_directory.rmdir()
        before = self.state_snapshot()

        with self.assertRaises(BootstrapRequiredError) as raised:
            with self.make_watcher():
                self.fail("missing checkpoint must prevent watcher startup")
        self.assertIn("from-start", str(raised.exception))
        self.assertIn("from-end", str(raised.exception))
        self.assertEqual(self.state_snapshot(), before)

    def test_v1_checkpoint_is_rejected_without_modifying_existing_state(self):
        checkpoint = self.prepare_checkpoint()
        legacy_data = checkpoint_to_dict(checkpoint)
        legacy_data.pop("source_prefix_sha256")
        legacy_data["version"] = 1
        self.checkpoint_path.write_text(json.dumps(legacy_data), encoding="utf-8")
        checkpoint_before = self.checkpoint_path.read_bytes()
        state_before = self.state_snapshot()

        with self.assertRaisesRegex(DurableWatcherError, "version 1"):
            with self.make_watcher():
                self.fail("v1 checkpoint must require an explicit migration")

        self.assertEqual(self.checkpoint_path.read_bytes(), checkpoint_before)
        self.assertEqual(self.state_snapshot(), state_before)
        self.assertFalse(self.lock_path.exists())

    def test_missing_checkpoint_with_existing_inbox_requires_manual_review(self):
        before = self.state_snapshot()

        with self.assertRaisesRegex(BootstrapRequiredError, "INCONSISTENT"):
            with self.make_watcher():
                self.fail("existing state must prevent automatic bootstrap")

        self.assertEqual(self.state_snapshot(), before)

    def test_corrupt_checkpoint_fails_closed(self):
        self.checkpoint_path.write_text("{broken", encoding="utf-8")
        before = self.state_snapshot()

        with self.assertRaisesRegex(DurableWatcherError, "CORRUPT"):
            with self.make_watcher():
                self.fail("corrupt checkpoint must prevent watcher startup")
        self.assertEqual(self.state_snapshot(), before)

    def test_missing_inbox_fails_without_recreating_it(self):
        self.inbox_directory.rmdir()
        self.prepare_checkpoint()
        checkpoint_before = self.checkpoint_path.read_bytes()
        state_before = self.state_snapshot()

        with self.assertRaisesRegex(DurableWatcherError, "INCONSISTENT"):
            with self.make_watcher():
                self.fail("missing Inbox must prevent watcher startup")

        self.assertFalse(self.inbox_directory.exists())
        self.assertEqual(self.checkpoint_path.read_bytes(), checkpoint_before)
        self.assertEqual(self.state_snapshot(), state_before)

    def test_missing_source_fails_without_creating_state(self):
        self.source_path.unlink()
        before = self.state_snapshot()

        with self.assertRaisesRegex(DurableWatcherError, "source log"):
            with self.make_watcher():
                self.fail("missing source must prevent watcher startup")

        self.assertEqual(self.state_snapshot(), before)

    def test_non_regular_source_fails_without_creating_state(self):
        before = self.state_snapshot()

        with self.assertRaisesRegex(DurableWatcherError, "regular file"):
            with DurableLogWatcher(
                source_path=self.source_directory,
                checkpoint_path=self.checkpoint_path,
                inbox_directory=self.inbox_directory,
                lock_path=self.lock_path,
                consumer_state_directory=self.state_directory / "consumer",
                consumer_lock_path=self.state_directory / "consumer.lock",
                rejected_file_path=self.state_directory / "rejected.jsonl",
            ):
                self.fail("directory source must prevent watcher startup")

        self.assertEqual(self.state_snapshot(), before)

    def test_checkpoint_not_at_record_boundary_fails_before_lock_creation(self):
        self.source_path.write_bytes(b"x\n")
        self.prepare_checkpoint(next_byte_offset=1)
        before = self.state_snapshot()

        with self.assertRaisesRegex(
            SourceIdentityMismatchError,
            "immediately after a complete LF record",
        ):
            with self.make_watcher():
                self.fail("checkpoint inside a record must prevent startup")

        self.assertEqual(self.state_snapshot(), before)

    def test_reads_complete_lf_record_and_preserves_exact_bytes(self):
        raw_record = self.record()
        self.source_path.write_bytes(raw_record)
        self.prepare_checkpoint()
        self.enable_test_directory_sync()

        with self.make_watcher() as watcher:
            self.assertEqual(watcher.poll_once(), 1)

        events = self.read_inbox_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].raw_record_bytes_b64,
            base64.b64encode(raw_record).decode("ascii"),
        )
        self.assertEqual(events[0].raw_record, raw_record.decode("utf-8"))
        self.assertEqual(events[0].byte_offset_start, 0)
        self.assertEqual(events[0].byte_offset_end, len(raw_record))
        self.assertEqual(
            load_checkpoint(self.checkpoint_path).next_byte_offset,
            len(raw_record),
        )
        self.assertEqual(
            load_checkpoint(self.checkpoint_path).source_prefix_sha256,
            hashlib.sha256(raw_record).hexdigest(),
        )

    def test_valid_append_and_restart_hash_only_the_committed_prefix(self):
        first_record = self.record()
        second_record = self.record("1123456789abcdef0123456789abcdef")
        self.source_path.write_bytes(first_record)
        self.prepare_checkpoint()
        self.enable_test_directory_sync()

        with self.make_watcher() as watcher:
            self.assertEqual(watcher.poll_once(), 1)

        checkpoint_after_first = load_checkpoint(self.checkpoint_path)
        self.assertEqual(
            checkpoint_after_first.source_prefix_sha256,
            hashlib.sha256(first_record).hexdigest(),
        )
        with self.source_path.open("ab") as source:
            source.write(second_record)

        with self.make_watcher() as watcher:
            self.assertEqual(watcher.poll_once(), 1)

        final_checkpoint = load_checkpoint(self.checkpoint_path)
        self.assertEqual(
            final_checkpoint.source_prefix_sha256,
            hashlib.sha256(first_record + second_record).hexdigest(),
        )
        self.assertEqual(final_checkpoint.next_byte_offset, len(first_record + second_record))
        self.assertEqual(len(self.read_inbox_events()), 2)

    def test_restart_recovers_pending_inbox_event_without_duplicate_file(self):
        raw_record = self.record()
        self.source_path.write_bytes(raw_record)
        self.prepare_checkpoint()
        source_identity = source_file_identity_from_stat(self.source_path.stat())
        inbox_event = create_inbox_event(
            source_identity,
            0,
            len(raw_record),
            raw_record,
        )
        with mock.patch.object(inbox_writer_module, "_fsync_directory"):
            inbox_writer_module.write_inbox_event(self.inbox_directory, inbox_event)
        self.enable_test_directory_sync()

        with self.make_watcher() as watcher:
            self.assertEqual(watcher.poll_once(), 1)

        checkpoint = load_checkpoint(self.checkpoint_path)
        self.assertEqual(checkpoint.next_byte_offset, len(raw_record))
        self.assertEqual(
            checkpoint.source_prefix_sha256,
            hashlib.sha256(raw_record).hexdigest(),
        )
        self.assertEqual(len(list(self.inbox_directory.glob("*.event.json"))), 1)

    def test_same_identity_truncate_regrow_fails_without_advancing_or_consuming_inbox(self):
        original_record = self.record()
        self.source_path.write_bytes(original_record)
        source_identity = source_file_identity_from_stat(self.source_path.stat())
        self.prepare_checkpoint(
            next_byte_offset=len(original_record),
            last_ingestion_key=None,
        )
        checkpoint_before = self.checkpoint_path.read_bytes()
        inbox_event = create_inbox_event(
            source_identity,
            0,
            len(original_record),
            original_record,
        )
        with mock.patch.object(inbox_writer_module, "_fsync_directory"):
            inbox_writer_module.write_inbox_event(self.inbox_directory, inbox_event)
        inbox_before = {
            path.name: path.read_bytes()
            for path in self.inbox_directory.glob("*.event.json")
        }

        rewritten_prefix = original_record.replace(b"192.0.2.10", b"192.0.2.99")
        self.source_path.write_bytes(
            rewritten_prefix + self.record("2123456789abcdef0123456789abcdef")
        )
        self.assertEqual(
            source_file_identity_from_stat(self.source_path.stat()),
            source_identity,
        )

        with self.assertRaisesRegex(SourceIdentityMismatchError, "SHA-256"):
            with self.make_watcher():
                self.fail("rewritten committed prefix must be rejected")

        self.assertEqual(self.checkpoint_path.read_bytes(), checkpoint_before)
        self.assertEqual(
            {
                path.name: path.read_bytes()
                for path in self.inbox_directory.glob("*.event.json")
            },
            inbox_before,
        )
        self.assertFalse((self.state_directory / "consumer").exists())

    def test_source_size_change_during_prefix_hash_fails_closed(self):
        first_record = self.record()
        second_record = self.record("1123456789abcdef0123456789abcdef")
        self.source_path.write_bytes(first_record + second_record)
        checkpoint = self.prepare_checkpoint(next_byte_offset=len(first_record))
        checkpoint_before = self.checkpoint_path.read_bytes()
        watcher = self.make_watcher()
        source_file = self.source_path.open("rb")
        watcher._source_file = source_file
        watcher._source_path_resolved = self.source_path.resolve()
        watcher._source_file_identity = checkpoint.source_file_identity
        watcher._next_byte_offset = checkpoint.next_byte_offset
        real_fstat = os.fstat
        fstat_attempts = 0

        def shrink_observed_size(file_descriptor):
            nonlocal fstat_attempts
            fstat_attempts += 1
            observed = real_fstat(file_descriptor)
            if fstat_attempts == 2:
                values = list(observed)
                values[6] = len(first_record)
                return os.stat_result(values)
            return observed

        with source_file, mock.patch.object(
            log_watcher.os, "fstat", side_effect=shrink_observed_size
        ):
            with self.assertRaisesRegex(
                SourceIdentityMismatchError,
                "size decreased while hashing",
            ):
                watcher._hash_checkpoint_prefix(checkpoint, source_file)

        self.assertEqual(fstat_attempts, 2)
        self.assertEqual(self.checkpoint_path.read_bytes(), checkpoint_before)
        self.assertEqual(list(self.inbox_directory.glob("*.event.json")), [])

    def test_efault_fstat_retries_once_and_recovers(self):
        raw_record = self.record()
        self.source_path.write_bytes(raw_record)
        self.prepare_checkpoint()
        self.enable_test_directory_sync()
        real_fstat = os.fstat
        attempts = 0

        def fail_fstat_once(file_descriptor):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError(errno.EFAULT, "simulated bad address")
            return real_fstat(file_descriptor)

        with self.make_watcher() as watcher:
            with mock.patch.object(
                log_watcher.os, "fstat", side_effect=fail_fstat_once
            ), mock.patch.object(log_watcher.time, "sleep") as sleep:
                self.assertEqual(watcher.poll_once(), 1)

        sleep.assert_called_once_with(0.25)
        self.assertGreaterEqual(attempts, 2)
        self.assertEqual(len(self.read_inbox_events()), 1)
        self.assertEqual(
            load_checkpoint(self.checkpoint_path).next_byte_offset,
            len(raw_record),
        )

    def test_efault_fstat_retries_during_checkpoint_validation(self):
        self.prepare_checkpoint()
        real_fstat = os.fstat
        attempts = 0

        def fail_fstat_once(file_descriptor):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError(errno.EFAULT, "simulated bad address")
            return real_fstat(file_descriptor)

        with mock.patch.object(
            log_watcher.os, "fstat", side_effect=fail_fstat_once
        ), mock.patch.object(log_watcher.time, "sleep") as sleep:
            with self.make_watcher():
                pass

        self.assertGreaterEqual(attempts, 2)
        sleep.assert_called_once_with(0.25)

    def test_efault_source_metadata_retries_during_startup(self):
        self.prepare_checkpoint()
        real_stat = os.stat
        attempts = 0

        def fail_source_stat_once(path, *args, **kwargs):
            nonlocal attempts
            if os.path.normcase(os.fspath(path)) == os.path.normcase(
                os.fspath(self.source_path)
            ) and attempts == 0:
                attempts += 1
                raise OSError(errno.EFAULT, "simulated bad address")
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(
            log_watcher.os, "stat", side_effect=fail_source_stat_once
        ), mock.patch.object(log_watcher.time, "sleep") as sleep:
            with self.make_watcher():
                pass

        self.assertEqual(attempts, 1)
        sleep.assert_called_once_with(0.25)

    def test_efault_source_metadata_retries_during_state_inspection(self):
        self.prepare_checkpoint()
        real_stat = os.stat
        source_stat_calls = 0

        def fail_source_stat_once_after_preflight(path, *args, **kwargs):
            nonlocal source_stat_calls
            if os.path.normcase(os.fspath(path)) == os.path.normcase(
                os.fspath(self.source_path)
            ):
                source_stat_calls += 1
                if source_stat_calls == 2:
                    raise OSError(errno.EFAULT, "simulated bad address")
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(
            log_watcher.os,
            "stat",
            side_effect=fail_source_stat_once_after_preflight,
        ), mock.patch.object(log_watcher.time, "sleep") as sleep:
            with self.make_watcher():
                pass

        self.assertGreaterEqual(source_stat_calls, 3)
        sleep.assert_called_once_with(0.25)

    def test_persistent_efault_source_metadata_fails_after_five_attempts(self):
        self.prepare_checkpoint()
        checkpoint_before = self.checkpoint_path.read_bytes()
        real_stat = os.stat
        attempts = 0

        def fail_source_stat(path, *args, **kwargs):
            nonlocal attempts
            if os.path.normcase(os.fspath(path)) == os.path.normcase(
                os.fspath(self.source_path)
            ):
                attempts += 1
                raise OSError(errno.EFAULT, "persistent bad address")
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(
            log_watcher.os, "stat", side_effect=fail_source_stat
        ), mock.patch.object(log_watcher.time, "sleep") as sleep:
            with self.assertRaisesRegex(
                SourceIOError,
                "startup source metadata inspection failed after 5 attempts",
            ):
                with self.make_watcher():
                    self.fail("persistent source metadata errors must fail closed")

        self.assertEqual(attempts, 5)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [0.25, 0.5, 1.0, 2.0],
        )
        self.assertEqual(self.checkpoint_path.read_bytes(), checkpoint_before)
        self.assertFalse(self.lock_path.exists())

    def test_persistent_efault_fstat_fails_with_shared_retry_budget(self):
        self.source_path.write_bytes(self.record())
        self.prepare_checkpoint()
        checkpoint_before = self.checkpoint_path.read_bytes()

        with self.make_watcher() as watcher:
            retry_budget = log_watcher._SourceIORetryBudget()
            with mock.patch.object(
                log_watcher.os,
                "fstat",
                side_effect=OSError(errno.EFAULT, "persistent bad address"),
            ) as fstat, mock.patch.object(log_watcher.time, "sleep") as sleep:
                with self.assertRaisesRegex(
                    SourceIOError, "readline failed after 5 attempts"
                ):
                    watcher._read_source_line(0, retry_budget)

        self.assertEqual(retry_budget.attempts, 5)
        self.assertEqual(fstat.call_count, 4)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [0.25, 0.5, 1.0],
        )
        self.assertEqual(self.checkpoint_path.read_bytes(), checkpoint_before)
        self.assertEqual(self.read_inbox_events(), [])

    def test_interleaved_outer_and_identity_errors_share_five_attempts(self):
        raw_record = self.record()
        self.source_path.write_bytes(raw_record)
        self.prepare_checkpoint()
        retry_budget = log_watcher._SourceIORetryBudget()
        real_fstat = os.fstat
        fstat_attempts = 0

        def fail_fstat_once(file_descriptor):
            nonlocal fstat_attempts
            fstat_attempts += 1
            if fstat_attempts == 1:
                raise OSError(errno.EFAULT, "identity failure")
            return real_fstat(file_descriptor)

        with self.make_watcher() as watcher:
            read_proxy = self.ReadFailureProxy(watcher._source_file)
            watcher._source_file = read_proxy
            with mock.patch.object(
                log_watcher.os, "fstat", side_effect=fail_fstat_once
            ), mock.patch.object(log_watcher.time, "sleep"):
                self.assertEqual(
                    watcher._read_source_line(0, retry_budget),
                    raw_record,
                )

        self.assertEqual(retry_budget.attempts, 5)
        self.assertEqual(fstat_attempts, 3)
        self.assertTrue(read_proxy.failed)
        self.assertEqual(read_proxy.readline_attempts, 2)

    def test_interleaved_seek_and_identity_errors_share_five_attempts(self):
        self.prepare_checkpoint()
        retry_budget = log_watcher._SourceIORetryBudget()
        real_fstat = os.fstat
        fstat_attempts = 0

        def fail_fstat_once(file_descriptor):
            nonlocal fstat_attempts
            fstat_attempts += 1
            if fstat_attempts == 1:
                raise OSError(errno.EFAULT, "identity failure")
            return real_fstat(file_descriptor)

        with self.make_watcher() as watcher:
            seek_proxy = self.ReadFailureProxy(
                watcher._source_file,
                fail_seek_once=True,
            )
            watcher._source_file = seek_proxy
            with mock.patch.object(
                log_watcher.os, "fstat", side_effect=fail_fstat_once
            ), mock.patch.object(log_watcher.time, "sleep"):
                watcher._seek_source(0, retry_budget)

        self.assertEqual(retry_budget.attempts, 5)
        self.assertEqual(fstat_attempts, 3)
        self.assertEqual(seek_proxy.seek_attempts, 2)

    def test_nested_retry_budget_stops_persistent_read_errors_safely(self):
        self.source_path.write_bytes(self.record())
        self.prepare_checkpoint()
        checkpoint_before = self.checkpoint_path.read_bytes()
        retry_budget = log_watcher._SourceIORetryBudget()

        with self.make_watcher() as watcher:
            read_proxy = self.ReadFailureProxy(
                watcher._source_file,
                persistent=True,
            )
            watcher._source_file = read_proxy
            with mock.patch.object(log_watcher.time, "sleep"):
                with self.assertRaisesRegex(
                    SourceIOError,
                    "readline exhausted the shared retry budget after 5 attempts",
                ):
                    watcher._read_source_line(0, retry_budget)

        self.assertEqual(retry_budget.attempts, 5)
        self.assertEqual(read_proxy.readline_attempts, 2)
        self.assertEqual(self.checkpoint_path.read_bytes(), checkpoint_before)
        self.assertEqual(self.read_inbox_events(), [])

    def test_retry_helper_supports_eintr_and_eagain(self):
        for error_number in (errno.EINTR, errno.EAGAIN):
            with self.subTest(error_number=error_number):
                attempts = 0

                def fail_once():
                    nonlocal attempts
                    attempts += 1
                    if attempts == 1:
                        raise OSError(error_number, "simulated transient error")
                    return "recovered"

                with mock.patch.object(log_watcher.time, "sleep") as sleep:
                    self.assertEqual(
                        log_watcher._retry_source_io(fail_once, "test operation"),
                        "recovered",
                    )

                self.assertEqual(attempts, 2)
                sleep.assert_called_once_with(0.25)

    def test_retry_helper_does_not_retry_non_transient_oserror(self):
        error = OSError(errno.EIO, "non-transient source error")

        def fail_non_transient():
            raise error

        with mock.patch.object(log_watcher.time, "sleep") as sleep:
            with self.assertRaisesRegex(OSError, "non-transient source error"):
                log_watcher._retry_source_io(fail_non_transient, "test")

        sleep.assert_not_called()

    def test_incomplete_record_is_retained_until_lf_arrives(self):
        raw_record = self.record()
        self.source_path.write_bytes(raw_record[:-1])
        self.prepare_checkpoint()
        self.enable_test_directory_sync()

        with self.make_watcher() as watcher:
            self.assertEqual(watcher.poll_once(), 0)
            self.assertEqual(watcher.next_byte_offset, 0)
            self.assertEqual(list(self.inbox_directory.glob("*.event.json")), [])
            self.assertEqual(load_checkpoint(self.checkpoint_path).next_byte_offset, 0)

            with self.source_path.open("ab") as source:
                source.write(b"\n")
                source.flush()
            self.assertEqual(watcher.poll_once(), 1)

        event = self.read_inbox_events()[0]
        self.assertEqual(
            event.raw_record_bytes_b64,
            base64.b64encode(raw_record).decode("ascii"),
        )
        self.assertEqual(load_checkpoint(self.checkpoint_path).next_byte_offset, len(raw_record))

    def test_inbox_is_written_before_checkpoint(self):
        raw_record = self.record()
        self.source_path.write_bytes(raw_record)
        self.prepare_checkpoint()
        self.enable_test_directory_sync()
        order = []
        real_write_inbox = log_watcher.write_inbox_event
        real_write_checkpoint = log_watcher.write_checkpoint

        def write_inbox(*args, **kwargs):
            order.append("inbox")
            return real_write_inbox(*args, **kwargs)

        def write_checkpoint(*args, **kwargs):
            order.append("checkpoint")
            return real_write_checkpoint(*args, **kwargs)

        with mock.patch.object(log_watcher, "write_inbox_event", side_effect=write_inbox):
            with mock.patch.object(
                log_watcher, "write_checkpoint", side_effect=write_checkpoint
            ):
                with self.make_watcher() as watcher:
                    watcher.poll_once()

        self.assertEqual(order, ["inbox", "checkpoint"])

    def test_inbox_failure_before_publication_does_not_advance_checkpoint(self):
        raw_record = self.record()
        self.source_path.write_bytes(raw_record)
        self.prepare_checkpoint()
        error = DurabilityError(
            "not published",
            operation="inbox_write",
            publication_state=PublicationState.NOT_PUBLISHED,
            directory_sync_state=DirectorySyncState.NOT_ATTEMPTED,
            destination=self.inbox_directory / "event.json",
        )

        with mock.patch.object(log_watcher, "write_inbox_event", side_effect=error):
            with self.make_watcher() as watcher:
                with self.assertRaises(DurabilityError):
                    watcher.poll_once()

        self.assertEqual(load_checkpoint(self.checkpoint_path).next_byte_offset, 0)
        self.assertEqual(list(self.inbox_directory.glob("*.event.json")), [])

    def test_inbox_failure_after_publication_keeps_event_and_checkpoint_unchanged(self):
        raw_record = self.record()
        self.source_path.write_bytes(raw_record)
        self.prepare_checkpoint()
        self.enable_test_directory_sync()
        real_write = log_watcher.write_inbox_event

        def publish_then_fail(directory, event):
            real_write(directory, event)
            raise DurabilityError(
                "published but not confirmed durable",
                operation="inbox_write",
                publication_state=PublicationState.PUBLISHED_UNCONFIRMED,
                directory_sync_state=DirectorySyncState.FAILED,
                destination=self.inbox_directory / "event.json",
            )

        with mock.patch.object(
            log_watcher, "write_inbox_event", side_effect=publish_then_fail
        ):
            with self.make_watcher() as watcher:
                with self.assertRaises(DurabilityError):
                    watcher.poll_once()

        self.assertEqual(load_checkpoint(self.checkpoint_path).next_byte_offset, 0)
        self.assertEqual(len(self.read_inbox_events()), 1)

    def test_checkpoint_failure_stops_watcher_and_keeps_durable_inbox(self):
        raw_record = self.record()
        self.source_path.write_bytes(raw_record)
        self.prepare_checkpoint()
        self.enable_test_directory_sync()
        original_checkpoint = self.checkpoint_path.read_bytes()

        with self.make_watcher() as watcher:
            with mock.patch.object(
                log_watcher,
                "write_checkpoint",
                side_effect=OSError("checkpoint unavailable"),
            ):
                with self.assertRaisesRegex(OSError, "checkpoint unavailable"):
                    watcher.poll_once()

            self.assertEqual(watcher.next_byte_offset, 0)
            self.assertEqual(self.checkpoint_path.read_bytes(), original_checkpoint)
            self.assertEqual(len(self.read_inbox_events()), 1)

            self.assertEqual(watcher.poll_once(), 1)

        self.assertEqual(len(self.read_inbox_events()), 1)
        self.assertEqual(
            load_checkpoint(self.checkpoint_path).next_byte_offset,
            len(raw_record),
        )

    def test_read_failure_retries_from_uncommitted_byte_boundary(self):
        raw_record = self.record()
        self.source_path.write_bytes(raw_record)
        self.prepare_checkpoint()
        self.enable_test_directory_sync()

        with self.make_watcher() as watcher:
            watcher._source_file = self.ReadFailureProxy(watcher._source_file)
            with mock.patch.object(log_watcher.time, "sleep") as sleep:
                self.assertEqual(watcher.poll_once(), 1)

        sleep.assert_called_once_with(0.25)
        events = self.read_inbox_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].byte_offset_start, 0)
        self.assertEqual(events[0].byte_offset_end, len(raw_record))
        self.assertEqual(
            events[0].raw_record_bytes_b64,
            base64.b64encode(raw_record).decode("ascii"),
        )
        self.assertEqual(
            load_checkpoint(self.checkpoint_path).next_byte_offset,
            len(raw_record),
        )

    def test_truncate_below_checkpoint_fails_closed(self):
        raw_record = self.record()
        self.source_path.write_bytes(raw_record)
        self.prepare_checkpoint(next_byte_offset=len(raw_record))
        checkpoint_before = self.checkpoint_path.read_bytes()
        self.source_path.write_bytes(raw_record[:-1])

        with self.assertRaises(SourceIdentityMismatchError):
            with self.make_watcher():
                self.fail("source truncation below committed offset must fail closed")

        self.assertEqual(self.checkpoint_path.read_bytes(), checkpoint_before)
        self.assertEqual(self.read_inbox_events(), [])

    def test_source_file_replacement_fails_closed(self):
        self.source_path.write_bytes(self.record())
        self.prepare_checkpoint()
        checkpoint_before = self.checkpoint_path.read_bytes()
        replacement_path = self.source_directory / "replacement.log"
        replacement_path.write_bytes(self.record("1123456789abcdef0123456789abcdef"))
        os.replace(replacement_path, self.source_path)

        with mock.patch.object(log_watcher.time, "sleep") as sleep:
            with self.assertRaises(SourceIdentityMismatchError):
                with self.make_watcher():
                    self.fail("source replacement must fail closed")

        sleep.assert_not_called()
        self.assertEqual(self.checkpoint_path.read_bytes(), checkpoint_before)
        self.assertEqual(self.read_inbox_events(), [])

    def test_two_processes_cannot_own_watcher_lock_concurrently(self):
        self.prepare_checkpoint()
        watcher = self.make_watcher()
        with watcher:
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
        self.assertEqual(completed.stdout.strip(), "CONTENDED")

    def test_source_identity_mismatch_fails_closed(self):
        self.prepare_checkpoint()
        checkpoint = load_checkpoint(self.checkpoint_path)
        wrong_checkpoint = Checkpoint(
            version=checkpoint.version,
            source_path=checkpoint.source_path,
            source_file_identity="stat-v1:999999:999999",
            next_byte_offset=checkpoint.next_byte_offset,
            last_ingestion_key=checkpoint.last_ingestion_key,
            updated_at=checkpoint.updated_at,
            source_prefix_sha256=checkpoint.source_prefix_sha256,
        )
        with mock.patch.object(checkpoint_module, "_fsync_directory"):
            write_checkpoint(self.checkpoint_path, wrong_checkpoint)
        before = self.state_snapshot()

        with mock.patch.object(log_watcher.time, "sleep") as sleep:
            with self.assertRaises(SourceIdentityMismatchError):
                with self.make_watcher():
                    self.fail("mismatched source identity must prevent startup")
        sleep.assert_not_called()
        self.assertEqual(self.state_snapshot(), before)

    def test_capture_preserves_raw_record_without_invoking_parser(self):
        raw_record = b'{"valid_json_but_invalid_schema":true}\n'
        self.source_path.write_bytes(raw_record)
        self.prepare_checkpoint()
        self.enable_test_directory_sync()

        with mock.patch.object(log_watcher, "parse_record", create=True) as parse:
            with self.make_watcher() as watcher:
                self.assertEqual(watcher.poll_once(), 1)

        event = self.read_inbox_events()[0]
        self.assertEqual(event.raw_record, raw_record.decode("utf-8"))
        self.assertIsNone(event.source_event_id)
        self.assertIs(event.parse_state, ParseState.UNVALIDATED)
        parse.assert_not_called()

    def test_watcher_capture_does_not_run_downstream_processing(self):
        raw_record = self.record()
        self.source_path.write_bytes(raw_record)
        self.prepare_checkpoint()
        self.enable_test_directory_sync()

        with mock.patch.object(
            log_watcher,
            "process_line",
            side_effect=RuntimeError("downstream failed"),
            create=True,
        ) as process_line:
            with self.make_watcher() as watcher:
                self.assertEqual(watcher.poll_once(), 1)
                process_line.assert_not_called()
                self.assertFalse(hasattr(watcher, "_pending_downstream"))

        self.assertEqual(load_checkpoint(self.checkpoint_path).next_byte_offset, len(raw_record))
        events = self.read_inbox_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].raw_record_bytes_b64,
            base64.b64encode(raw_record).decode("ascii"),
        )

    def test_run_forever_consumes_the_configured_inbox_after_capture(self):
        raw_record = self.record()
        self.source_path.write_bytes(raw_record)
        self.prepare_checkpoint()
        self.enable_test_directory_sync()
        consumer = mock.Mock()
        stop_loop = RuntimeError("stop after first iteration")

        with mock.patch.object(
            log_watcher, "InboxConsumer", return_value=consumer
        ) as consumer_factory, mock.patch.object(
            log_watcher.time, "sleep", side_effect=stop_loop
        ):
            with self.assertRaisesRegex(RuntimeError, "stop after first iteration"):
                self.make_watcher().run_forever()

        self.assertEqual(
            consumer_factory.call_args.kwargs["inbox_directory"],
            self.inbox_directory,
        )
        consumer.consume_once.assert_called_once_with()
        self.assertEqual(len(self.read_inbox_events()), 1)

    def test_multiple_records_have_contiguous_offsets(self):
        records = [
            self.record("0123456789abcdef0123456789abcdef"),
            self.record("1123456789abcdef0123456789abcdef"),
            self.record("2123456789abcdef0123456789abcdef"),
        ]
        raw_source = b"".join(records)
        self.source_path.write_bytes(raw_source)
        self.prepare_checkpoint()
        self.enable_test_directory_sync()

        with self.make_watcher() as watcher:
            self.assertEqual(watcher.poll_once(), len(records))

        events = sorted(
            self.read_inbox_events(), key=lambda event: event.byte_offset_start
        )
        expected_offset = 0
        for event, raw_record in zip(events, records):
            self.assertEqual(event.byte_offset_start, expected_offset)
            self.assertEqual(
                event.byte_offset_end - event.byte_offset_start, len(raw_record)
            )
            expected_offset = event.byte_offset_end
        self.assertEqual(expected_offset, len(raw_source))
        checkpoint = load_checkpoint(self.checkpoint_path)
        self.assertEqual(checkpoint.next_byte_offset, len(raw_source))
        self.assertEqual(checkpoint.last_ingestion_key, events[-1].ingestion_key)


if __name__ == "__main__":
    unittest.main()
