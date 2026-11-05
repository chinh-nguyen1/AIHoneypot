import os
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock


PIPELINE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE_DIR))

import inbox_writer
from inbox_record import (
    InboxRecordError,
    ParseState,
    create_inbox_event,
    inbox_event_filename,
    inbox_event_from_json,
)
from inbox_writer import (
    InboxCleanupError,
    InboxEventConflictError,
    InboxWriteStatus,
    write_inbox_event,
)
from durability_contract import (
    DirectorySyncState,
    DurabilityError,
    PublicationState,
)


class InboxWriterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.directory = Path(self.temp_dir.name) / "inbox"
        raw_bytes = '{"payload":"Xin chào"}\r\n'.encode("utf-8")
        self.event = create_inbox_event(
            "fixture-generation",
            0,
            len(raw_bytes),
            raw_bytes,
            source_event_id="test-event-id",
            ingested_at="2026-10-31T12:00:00+00:00",
        )

    def _write_with_directory_sync_mocked(self, event=None):
        with mock.patch.object(inbox_writer, "_fsync_directory"):
            return write_inbox_event(self.directory, event or self.event)

    def test_writes_event_and_round_trips(self):
        result = self._write_with_directory_sync_mocked()

        self.assertEqual(result.status, InboxWriteStatus.CREATED)
        self.assertFalse(result.already_exists)
        self.assertEqual(result.outcome.publication_state, PublicationState.DURABLE)
        self.assertEqual(result.outcome.directory_sync_state, DirectorySyncState.SYNCED)
        self.assertTrue(result.outcome.is_durable_success)
        self.assertTrue(result.path.is_file())
        self.assertEqual(
            inbox_event_from_json(result.path.read_text(encoding="utf-8")),
            self.event,
        )

    def test_duplicate_write_returns_already_exists_without_overwrite(self):
        first = self._write_with_directory_sync_mocked()
        before = first.path.read_bytes()
        second = self._write_with_directory_sync_mocked()

        self.assertEqual(second.status, InboxWriteStatus.ALREADY_EXISTS)
        self.assertTrue(second.already_exists)
        self.assertTrue(second.outcome.is_durable_success)
        self.assertEqual(second.path.read_bytes(), before)

    def test_identical_raw_records_at_different_offsets_remain_distinct_events(self):
        raw_bytes = b'{"payload":"same request"}\n'
        first_event = create_inbox_event(
            "fixture-generation",
            0,
            len(raw_bytes),
            raw_bytes,
        )
        second_event = create_inbox_event(
            "fixture-generation",
            len(raw_bytes),
            len(raw_bytes) * 2,
            raw_bytes,
        )

        with mock.patch.object(inbox_writer, "_fsync_directory"):
            first_result = write_inbox_event(self.directory, first_event)
            second_result = write_inbox_event(self.directory, second_event)

        self.assertNotEqual(first_event.ingestion_key, second_event.ingestion_key)
        self.assertNotEqual(first_result.path, second_result.path)
        self.assertTrue(first_result.path.is_file())
        self.assertTrue(second_result.path.is_file())

    def test_matching_duplicate_requires_successful_directory_sync(self):
        self._write_with_directory_sync_mocked()
        with mock.patch.object(inbox_writer, "_fsync_directory") as sync:
            result = write_inbox_event(self.directory, self.event)

        self.assertEqual(result.status, InboxWriteStatus.ALREADY_EXISTS)
        self.assertTrue(result.outcome.is_durable_success)
        sync.assert_called_once_with(self.directory)

    def test_matching_duplicate_with_unsupported_sync_is_unconfirmed(self):
        self._write_with_directory_sync_mocked()
        with mock.patch.object(
            inbox_writer,
            "_fsync_directory",
            side_effect=NotImplementedError("unsupported"),
        ):
            with self.assertRaises(DurabilityError) as raised:
                write_inbox_event(self.directory, self.event)

        self.assertTrue(raised.exception.published)
        self.assertEqual(
            raised.exception.publication_state,
            PublicationState.PUBLISHED_UNCONFIRMED,
        )
        self.assertEqual(
            raised.exception.directory_sync_state,
            DirectorySyncState.UNSUPPORTED,
        )

    def test_same_key_with_different_raw_bytes_raises_without_modifying_target(self):
        self._write_with_directory_sync_mocked()
        destination = self.directory / inbox_event_filename(self.event.ingestion_key)
        before = destination.read_bytes()
        conflicting_event = create_inbox_event(
            "fixture-generation",
            self.event.byte_offset_start,
            self.event.byte_offset_end,
            b"x" * (self.event.byte_offset_end - self.event.byte_offset_start),
            ingested_at="2026-10-31T12:00:00+00:00",
        )

        with mock.patch.object(inbox_writer, "_fsync_directory"):
            with self.assertRaises(InboxEventConflictError):
                write_inbox_event(self.directory, conflicting_event)

        self.assertEqual(destination.read_bytes(), before)

    def test_corrupt_existing_event_raises_without_overwrite(self):
        self.directory.mkdir(parents=True)
        destination = self.directory / inbox_event_filename(self.event.ingestion_key)
        destination.write_text("{broken", encoding="utf-8")

        with mock.patch.object(inbox_writer, "_fsync_directory"):
            with self.assertRaises(InboxEventConflictError):
                write_inbox_event(self.directory, self.event)

        self.assertEqual(destination.read_text(encoding="utf-8"), "{broken")

    def test_temp_file_is_created_in_destination_directory(self):
        observed = {}
        real_mkstemp = tempfile.mkstemp

        def capture_mkstemp(*args, **kwargs):
            observed["dir"] = Path(kwargs["dir"])
            return real_mkstemp(*args, **kwargs)

        with mock.patch.object(inbox_writer.tempfile, "mkstemp", side_effect=capture_mkstemp):
            self._write_with_directory_sync_mocked()

        self.assertEqual(observed["dir"], self.directory)
        self.assertEqual(list(self.directory.glob(".*.tmp")), [])

    def test_write_failure_does_not_report_success_and_preserves_temp(self):
        with mock.patch.object(
            inbox_writer, "_write_temp_file", side_effect=OSError("write failed")
        ):
            with self.assertRaises(DurabilityError) as raised:
                write_inbox_event(self.directory, self.event)

        self.assertIsInstance(raised.exception.original_cause, OSError)
        self.assertTrue(raised.exception.before_publication)
        self.assertEqual(
            raised.exception.publication_state,
            PublicationState.NOT_PUBLISHED,
        )
        self.assertEqual(
            raised.exception.directory_sync_state,
            DirectorySyncState.NOT_ATTEMPTED,
        )
        self.assertEqual(len(list(self.directory.glob(".*.tmp"))), 1)
        self.assertFalse(
            (self.directory / inbox_event_filename(self.event.ingestion_key)).exists()
        )

    def test_file_fsync_failure_does_not_report_success_and_preserves_temp(self):
        with mock.patch.object(os, "fsync", side_effect=OSError("file fsync failed")):
            with self.assertRaises(DurabilityError) as raised:
                write_inbox_event(self.directory, self.event)

        self.assertIsInstance(raised.exception.original_cause, OSError)
        self.assertFalse(raised.exception.published)
        self.assertEqual(len(list(self.directory.glob(".*.tmp"))), 1)

    def test_flush_failure_does_not_report_success_and_preserves_temp(self):
        class FlushFailure:
            def __init__(self, file_descriptor):
                self.file_descriptor = file_descriptor

            def __enter__(self):
                return self

            def __exit__(self, *args):
                del args
                return False

            def write(self, content):
                return len(content)

            def flush(self):
                raise OSError("flush failed")

            def fileno(self):
                return self.file_descriptor

            def record_fdopen_arguments(self, mode, closefd):
                self.mode = mode
                self.closefd = closefd

        def fake_fdopen(file_descriptor, mode, closefd):
            failure = FlushFailure(file_descriptor)
            failure.record_fdopen_arguments(mode, closefd)
            return failure

        with mock.patch.object(
            inbox_writer.os,
            "fdopen",
            side_effect=fake_fdopen,
        ):
            with self.assertRaises(DurabilityError) as raised:
                write_inbox_event(self.directory, self.event)

        self.assertEqual(str(raised.exception.original_cause), "flush failed")
        self.assertFalse(raised.exception.published)
        self.assertEqual(len(list(self.directory.glob(".*.tmp"))), 1)
        self.assertFalse(
            (self.directory / inbox_event_filename(self.event.ingestion_key)).exists()
        )

    def test_publish_failure_does_not_report_success_and_preserves_temp(self):
        with mock.patch.object(
            inbox_writer,
            "_publish_no_replace",
            side_effect=OSError("publish failed"),
        ):
            with self.assertRaises(DurabilityError) as raised:
                write_inbox_event(self.directory, self.event)

        self.assertTrue(raised.exception.before_publication)
        self.assertEqual(str(raised.exception.original_cause), "publish failed")
        self.assertEqual(len(list(self.directory.glob(".*.tmp"))), 1)

    def test_temp_creation_failure_is_not_published(self):
        self.directory.mkdir(parents=True)
        cause = OSError("temp create failed")
        with mock.patch.object(
            inbox_writer.tempfile,
            "mkstemp",
            side_effect=cause,
        ):
            with self.assertRaises(DurabilityError) as raised:
                write_inbox_event(self.directory, self.event)

        self.assertIs(raised.exception.original_cause, cause)
        self.assertFalse(raised.exception.published)
        self.assertEqual(
            raised.exception.directory_sync_state,
            DirectorySyncState.NOT_ATTEMPTED,
        )
        self.assertFalse(
            (self.directory / inbox_event_filename(self.event.ingestion_key)).exists()
        )

    def test_directory_sync_success_reports_durable(self):
        sync_calls = []
        with mock.patch.object(
            inbox_writer,
            "_fsync_directory",
            side_effect=lambda directory: sync_calls.append(directory),
        ):
            result = write_inbox_event(self.directory, self.event)

        self.assertEqual(result.outcome.publication_state, PublicationState.DURABLE)
        self.assertEqual(result.outcome.directory_sync_state, DirectorySyncState.SYNCED)
        self.assertTrue(result.outcome.is_durable_success)
        self.assertGreaterEqual(len(sync_calls), 1)

    def test_directory_sync_unsupported_is_published_unconfirmed(self):
        with mock.patch.object(
            inbox_writer,
            "_fsync_directory",
            side_effect=NotImplementedError("unsupported"),
        ):
            with self.assertRaises(DurabilityError) as raised:
                write_inbox_event(self.directory, self.event)

        self.assertTrue(raised.exception.published)
        self.assertEqual(
            raised.exception.publication_state,
            PublicationState.PUBLISHED_UNCONFIRMED,
        )
        self.assertEqual(
            raised.exception.directory_sync_state,
            DirectorySyncState.UNSUPPORTED,
        )
        self.assertIsInstance(raised.exception.original_cause, NotImplementedError)
        self.assertTrue(
            (self.directory / inbox_event_filename(self.event.ingestion_key)).is_file()
        )
        if getattr(os, "name", "") == "nt":
            self.assertEqual(len(list(self.directory.glob(".*.tmp"))), 0)
        else:
            self.assertEqual(len(list(self.directory.glob(".*.tmp"))), 1)

    def test_directory_sync_failure_is_published_unconfirmed(self):
        cause = OSError("directory sync failed")
        with mock.patch.object(
            inbox_writer,
            "_fsync_directory",
            side_effect=cause,
        ):
            with self.assertRaises(DurabilityError) as raised:
                write_inbox_event(self.directory, self.event)

        self.assertIs(raised.exception.original_cause, cause)
        self.assertTrue(raised.exception.after_publication)
        self.assertEqual(
            raised.exception.publication_state,
            PublicationState.PUBLISHED_UNCONFIRMED,
        )
        self.assertEqual(
            raised.exception.directory_sync_state,
            DirectorySyncState.FAILED,
        )
        destination = self.directory / inbox_event_filename(self.event.ingestion_key)
        self.assertTrue(destination.is_file())
        self.assertEqual(
            inbox_event_from_json(destination.read_text(encoding="utf-8")),
            self.event,
        )
        if getattr(os, "name", "") != "nt":
            self.assertEqual(len(list(self.directory.glob(".*.tmp"))), 1)

    @unittest.skipIf(os.name == "nt", "POSIX hard-link cleanup sequence")
    def test_cleanup_failure_after_first_sync_preserves_durable_outcome(self):
        sync_calls = []
        real_link = os.link
        temp_path_holder = []

        def link_and_record_temp(source, destination):
            temp_path_holder.append(Path(source))
            return real_link(source, destination)

        def sync_directory(directory):
            sync_calls.append(directory)

        cause = OSError("temporary link cleanup failed")
        with mock.patch.object(
            inbox_writer, "_publish_no_replace", side_effect=link_and_record_temp
        ):
            with mock.patch.object(
                inbox_writer, "_fsync_directory", side_effect=sync_directory
            ):
                with mock.patch.object(os, "unlink", side_effect=cause):
                    with self.assertRaises(InboxCleanupError) as raised:
                        write_inbox_event(self.directory, self.event)

        self.assertEqual(len(sync_calls), 1)
        self.assertEqual(raised.exception.publication_state, PublicationState.DURABLE)
        self.assertEqual(
            raised.exception.directory_sync_state,
            DirectorySyncState.SYNCED,
        )
        self.assertEqual(raised.exception.cleanup_stage, "unlink_temp")
        self.assertIs(raised.exception.original_cause, cause)
        self.assertTrue(raised.exception.destination.is_file())
        self.assertTrue(raised.exception.temporary_path.is_file())
        self.assertTrue(temp_path_holder)

    @unittest.skipIf(os.name == "nt", "POSIX second directory sync sequence")
    def test_second_directory_sync_failure_keeps_first_durability_ack(self):
        real_link = os.link
        sync_calls = []

        def sync_directory(directory):
            sync_calls.append(directory)
            if len(sync_calls) == 2:
                raise OSError("second directory sync failed")

        with mock.patch.object(
            inbox_writer,
            "_publish_no_replace",
            side_effect=lambda source, destination: (
                real_link(source, destination) or True
            ),
        ):
            with mock.patch.object(
                inbox_writer, "_fsync_directory", side_effect=sync_directory
            ):
                with self.assertRaises(InboxCleanupError) as raised:
                    write_inbox_event(self.directory, self.event)

        self.assertEqual(len(sync_calls), 2)
        self.assertEqual(raised.exception.publication_state, PublicationState.DURABLE)
        self.assertEqual(
            raised.exception.directory_sync_state,
            DirectorySyncState.SYNCED,
        )
        self.assertEqual(
            raised.exception.cleanup_stage,
            "fsync_directory_after_temp_unlink",
        )
        self.assertEqual(
            str(raised.exception.original_cause),
            "second directory sync failed",
        )
        self.assertTrue(raised.exception.destination.is_file())
        self.assertFalse(raised.exception.temporary_path.exists())

    def test_same_filename_race_is_no_clobber_and_returns_duplicate(self):
        barrier = threading.Barrier(2)
        results = []
        failures = []

        def concurrent_write():
            try:
                barrier.wait(timeout=5)
                results.append(write_inbox_event(self.directory, self.event))
            except BaseException as exc:
                failures.append(exc)

        threads = [threading.Thread(target=concurrent_write) for _ in range(2)]
        with mock.patch.object(inbox_writer, "_fsync_directory"):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertCountEqual(
            [result.status for result in results],
            [InboxWriteStatus.CREATED, InboxWriteStatus.ALREADY_EXISTS],
        )
        self.assertEqual(
            len(list(self.directory.glob("*.event.json"))),
            1,
        )

    def test_invalid_event_contract_is_rejected_before_filesystem_write(self):
        invalid_event = replace(self.event, parse_state=ParseState.QUARANTINED)
        with self.assertRaises(InboxRecordError):
            write_inbox_event(self.directory, invalid_event)
        self.assertFalse(self.directory.exists())


if __name__ == "__main__":
    unittest.main()
