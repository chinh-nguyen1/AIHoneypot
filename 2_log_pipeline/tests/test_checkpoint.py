import errno
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PIPELINE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE_DIR))

import checkpoint as checkpoint_module
from checkpoint import (
    Checkpoint,
    CheckpointCorruptionError,
    CheckpointNotFoundError,
    CheckpointSourceError,
    CheckpointValidationError,
    CheckpointWriteError,
    EMPTY_SOURCE_PREFIX_SHA256,
    LegacyCheckpointError,
    CHECKPOINT_VERSION,
    hash_source_prefix,
    checkpoint_from_json,
    checkpoint_to_json,
    load_checkpoint,
    write_checkpoint,
)
from durability_contract import (
    DirectorySyncState,
    DurabilityError,
    PublicationState,
)
from inbox_record import create_ingestion_key


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.directory = Path(self.temp_dir.name)
        self.path = self.directory / "state" / "checkpoint.json"
        self.checkpoint = Checkpoint(
            version=CHECKPOINT_VERSION,
            source_path=str(self.directory / "logs" / "access.log"),
            source_file_identity="generation-α",
            next_byte_offset=27,
            last_ingestion_key=create_ingestion_key("generation-α", 10, 27),
            updated_at="2026-10-31T12:00:00+00:00",
            source_prefix_sha256="0" * 64,
        )

    def data(self):
        return {
            "version": CHECKPOINT_VERSION,
            "source_path": self.checkpoint.source_path,
            "source_file_identity": "generation-α",
            "next_byte_offset": 27,
            "last_ingestion_key": create_ingestion_key("generation-α", 10, 27),
            "updated_at": "2026-10-31T12:00:00+00:00",
            "source_prefix_sha256": "0" * 64,
        }

    def checkpoint_from_updates(self, **updates):
        data = self.data()
        data.update(updates)
        return Checkpoint(**data)

    def test_valid_checkpoint(self):
        self.assertEqual(self.checkpoint.version, 2)

    def test_rejects_empty_or_relative_source_paths(self):
        for path in ("", "   ", "relative/access.log"):
            with self.subTest(path=path):
                with self.assertRaises(CheckpointValidationError):
                    self.checkpoint_from_updates(source_path=path)

    def test_rejects_empty_source_identity(self):
        with self.assertRaises(CheckpointValidationError):
            self.checkpoint_from_updates(source_file_identity=" \t ")

    def test_rejects_negative_or_boolean_offsets(self):
        for offset in (-1, True):
            with self.subTest(offset=offset):
                with self.assertRaises(CheckpointValidationError):
                    self.checkpoint_from_updates(next_byte_offset=offset)

    def test_rejects_boolean_or_unsupported_version(self):
        for version in (True, 1, 3):
            with self.subTest(version=version):
                with self.assertRaises(CheckpointValidationError):
                    self.checkpoint_from_updates(version=version)

    def test_rejects_invalid_and_timezone_naive_timestamps(self):
        for timestamp in ("not-a-date", "2026-10-31T12:00:00"):
            with self.subTest(timestamp=timestamp):
                with self.assertRaises(CheckpointValidationError):
                    self.checkpoint_from_updates(updated_at=timestamp)

    def test_rejects_invalid_ingestion_keys(self):
        invalid_keys = (
            "",
            "not-json",
            '["generation-α",10,26]',
            '["other-generation",10,27]',
            '["generation-α",true,27]',
            '["generation-α",10,27.0]',
            '["generation-α",10,27] ',
        )
        for key in invalid_keys:
            with self.subTest(key=key):
                with self.assertRaises(CheckpointValidationError):
                    self.checkpoint_from_updates(last_ingestion_key=key)

    def test_nullable_ingestion_key_accepts_initial_checkpoint_offset(self):
        checkpoint = self.checkpoint_from_updates(
            next_byte_offset=128,
            last_ingestion_key=None,
        )
        self.assertIsNone(checkpoint.last_ingestion_key)

    def test_empty_prefix_requires_canonical_empty_digest(self):
        checkpoint = self.checkpoint_from_updates(
            next_byte_offset=0,
            last_ingestion_key=None,
            source_prefix_sha256=EMPTY_SOURCE_PREFIX_SHA256,
        )
        self.assertEqual(checkpoint.next_byte_offset, 0)
        with self.assertRaisesRegex(CheckpointValidationError, "empty digest"):
            self.checkpoint_from_updates(
                next_byte_offset=0,
                last_ingestion_key=None,
                source_prefix_sha256="0" * 64,
            )

    def test_rejects_malformed_source_prefix_digest(self):
        for digest in (None, "0" * 63, "0" * 65, "A" * 64, "g" * 64):
            with self.subTest(digest=digest):
                with self.assertRaises(CheckpointValidationError):
                    self.checkpoint_from_updates(source_prefix_sha256=digest)

    def test_hash_source_prefix_preserves_raw_newline_bytes(self):
        raw_source = b"first\r\nsecond\n"
        source_file = io.BytesIO(raw_source)

        digest, last_byte, _ = hash_source_prefix(
            source_file,
            len(b"first\r\n"),
            chunk_size=2,
        )

        self.assertEqual(digest, hashlib.sha256(b"first\r\n").hexdigest())
        self.assertEqual(last_byte, b"\n")
        self.assertEqual(source_file.tell(), len(b"first\r\n"))

    def test_hash_source_prefix_fails_if_source_ends_before_offset(self):
        with self.assertRaisesRegex(CheckpointSourceError, "ended before"):
            hash_source_prefix(io.BytesIO(b"short"), 6, chunk_size=2)

    def test_deterministic_json_round_trip(self):
        serialized = checkpoint_to_json(self.checkpoint)
        self.assertEqual(serialized, checkpoint_to_json(self.checkpoint))
        self.assertEqual(checkpoint_from_json(serialized), self.checkpoint)
        self.assertIn("generation-α", serialized)

    def test_deserializer_rejects_missing_and_unexpected_fields(self):
        missing = self.data()
        del missing["version"]
        unexpected = {**self.data(), "extra": "value"}
        for data in (missing, unexpected):
            with self.subTest(data=data):
                with self.assertRaises(CheckpointCorruptionError):
                    checkpoint_from_json(json.dumps(data))

    def test_valid_v1_checkpoint_is_recognized_but_rejected_for_migration(self):
        legacy = self.data()
        legacy.pop("source_prefix_sha256")
        legacy["version"] = 1
        with self.assertRaisesRegex(LegacyCheckpointError, "explicit"):
            checkpoint_from_json(json.dumps(legacy))

    def test_deserializer_rejects_invalid_json_and_non_finite_numbers(self):
        for serialized in (
            "{",
            json.dumps({**self.data(), "next_byte_offset": float("nan")}),
            json.dumps({**self.data(), "next_byte_offset": float("inf")}),
            json.dumps({**self.data(), "next_byte_offset": float("-inf")}),
            json.dumps(self.data()).replace(
                '"next_byte_offset": 27',
                '"next_byte_offset": NaN',
            ),
        ):
            with self.subTest(serialized=serialized):
                with self.assertRaises(CheckpointCorruptionError):
                    checkpoint_from_json(serialized)

    def test_deserializer_rejects_duplicate_fields(self):
        serialized = checkpoint_to_json(self.checkpoint)
        with self.assertRaisesRegex(CheckpointCorruptionError, "duplicate"):
            checkpoint_from_json(serialized[:-1] + ',"version":1}')

    def test_creates_and_loads_new_checkpoint(self):
        result = self._write_with_directory_sync_mocked(self.checkpoint)

        self.assertTrue(result.path.is_file())
        self.assertTrue(result.directory_fsynced)
        self.assertIsNone(result.durability_note)
        self.assertEqual(
            result.outcome.publication_state,
            PublicationState.DURABLE,
        )
        self.assertEqual(
            result.outcome.directory_sync_state,
            DirectorySyncState.SYNCED,
        )
        self.assertTrue(result.outcome.is_durable_success)
        self.assertEqual(load_checkpoint(self.path), self.checkpoint)

    def test_replaces_existing_checkpoint_atomically(self):
        original = self._write_with_directory_sync_mocked(self.checkpoint)
        newer = self.checkpoint_from_updates(
            next_byte_offset=40,
            last_ingestion_key=create_ingestion_key("generation-α", 27, 40),
        )
        updated = self._write_with_directory_sync_mocked(newer)

        self.assertEqual(original.path, updated.path)
        self.assertEqual(load_checkpoint(self.path), newer)

    def test_temporary_file_is_created_in_destination_directory(self):
        observed = {}
        actual_mkstemp = tempfile.mkstemp

        def capture_mkstemp(*args, **kwargs):
            observed["directory"] = Path(kwargs["dir"])
            return actual_mkstemp(*args, **kwargs)

        with mock.patch.object(checkpoint_module.tempfile, "mkstemp", side_effect=capture_mkstemp):
            self._write_with_directory_sync_mocked(self.checkpoint)

        self.assertEqual(observed["directory"], self.path.parent)
        self.assertEqual(list(self.path.parent.glob(".*.tmp")), [])

    def test_temp_creation_failure_is_not_published_and_preserves_cause(self):
        self.path.parent.mkdir(parents=True)
        cause = OSError("temp create failed")
        with mock.patch.object(
            checkpoint_module.tempfile,
            "mkstemp",
            side_effect=cause,
        ):
            with self.assertRaises(CheckpointWriteError) as raised:
                write_checkpoint(self.path, self.checkpoint)

        self.assertIs(raised.exception.original_cause, cause)
        self.assertIs(raised.exception.__cause__, cause)
        self.assertEqual(
            raised.exception.publication_state,
            PublicationState.NOT_PUBLISHED,
        )
        self.assertEqual(
            raised.exception.directory_sync_state,
            DirectorySyncState.NOT_ATTEMPTED,
        )
        self.assertFalse(raised.exception.replacement_completed)

    def test_checkpoint_directory_creation_failure_is_not_published(self):
        cause = OSError("directory create failed")
        with mock.patch.object(Path, "mkdir", side_effect=cause):
            with self.assertRaises(CheckpointWriteError) as raised:
                write_checkpoint(self.path, self.checkpoint)

        self.assertIs(raised.exception.original_cause, cause)
        self.assertEqual(
            raised.exception.publication_state,
            PublicationState.NOT_PUBLISHED,
        )
        self.assertEqual(
            raised.exception.directory_sync_state,
            DirectorySyncState.NOT_ATTEMPTED,
        )
        self.assertFalse(self.path.exists())

    def test_json_serialization_failure_is_not_published(self):
        cause = TypeError("serialization failed")
        with mock.patch.object(
            checkpoint_module,
            "checkpoint_to_json",
            side_effect=cause,
        ):
            with self.assertRaises(CheckpointWriteError) as raised:
                write_checkpoint(self.path, self.checkpoint)

        self.assertIs(raised.exception.original_cause, cause)
        self.assertFalse(raised.exception.published)
        self.assertFalse(self.path.exists())

    def test_write_failure_preserves_previous_checkpoint_and_temp(self):
        old = self._write_with_directory_sync_mocked(self.checkpoint)
        previous_bytes = old.path.read_bytes()
        newer = self.checkpoint_from_updates(
            next_byte_offset=40,
            last_ingestion_key=create_ingestion_key("generation-α", 27, 40),
        )
        cause = OSError("write failed")
        with mock.patch.object(
            checkpoint_module, "_write_temp_file", side_effect=cause
        ):
            with self.assertRaises(CheckpointWriteError) as raised:
                write_checkpoint(self.path, newer)

        self.assertIs(raised.exception.original_cause, cause)
        self.assertFalse(raised.exception.replacement_completed)
        self.assertEqual(
            raised.exception.directory_sync_state,
            DirectorySyncState.NOT_ATTEMPTED,
        )
        self.assertEqual(self.path.read_bytes(), previous_bytes)
        self.assertEqual(len(list(self.path.parent.glob(".*.tmp"))), 1)

    def test_flush_failure_preserves_previous_checkpoint(self):
        old = self._write_with_directory_sync_mocked(self.checkpoint)
        previous_bytes = old.path.read_bytes()
        newer = self.checkpoint_from_updates(
            next_byte_offset=40,
            last_ingestion_key=create_ingestion_key("generation-α", 27, 40),
        )

        class FlushFailure:
            def __init__(self, descriptor):
                self.descriptor = descriptor

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
                return self.descriptor

        def fake_fdopen(descriptor, mode, closefd):
            del mode, closefd
            return FlushFailure(descriptor)

        with mock.patch.object(
            checkpoint_module.os,
            "fdopen",
            side_effect=fake_fdopen,
        ):
            with self.assertRaises(CheckpointWriteError) as raised:
                write_checkpoint(self.path, newer)

        self.assertEqual(str(raised.exception.original_cause), "flush failed")
        self.assertEqual(
            raised.exception.publication_state,
            PublicationState.NOT_PUBLISHED,
        )
        self.assertFalse(raised.exception.replacement_completed)
        self.assertEqual(self.path.read_bytes(), previous_bytes)
        self.assertEqual(len(list(self.path.parent.glob(".*.tmp"))), 1)

    def test_file_fsync_failure_preserves_previous_checkpoint(self):
        old = self._write_with_directory_sync_mocked(self.checkpoint)
        previous_bytes = old.path.read_bytes()
        newer = self.checkpoint_from_updates(
            next_byte_offset=40,
            last_ingestion_key=create_ingestion_key("generation-α", 27, 40),
        )
        cause = OSError("file fsync failed")
        with mock.patch.object(os, "fsync", side_effect=cause):
            with self.assertRaises(CheckpointWriteError) as raised:
                write_checkpoint(self.path, newer)

        self.assertIs(raised.exception.original_cause, cause)
        self.assertFalse(raised.exception.published)
        self.assertEqual(
            raised.exception.directory_sync_state,
            DirectorySyncState.NOT_ATTEMPTED,
        )
        self.assertEqual(self.path.read_bytes(), previous_bytes)
        self.assertEqual(len(list(self.path.parent.glob(".*.tmp"))), 1)

    def test_atomic_replace_failure_preserves_previous_checkpoint(self):
        self._write_with_directory_sync_mocked(self.checkpoint)
        previous = load_checkpoint(self.path)
        newer = self.checkpoint_from_updates(
            next_byte_offset=40,
            last_ingestion_key=create_ingestion_key("generation-α", 27, 40),
        )
        cause = OSError("replace failed")
        with mock.patch.object(os, "replace", side_effect=cause):
            with self.assertRaises(CheckpointWriteError) as raised:
                write_checkpoint(self.path, newer)

        self.assertIs(raised.exception.original_cause, cause)
        self.assertIs(raised.exception.__cause__, cause)
        self.assertEqual(
            raised.exception.publication_state,
            PublicationState.NOT_PUBLISHED,
        )
        self.assertEqual(
            raised.exception.directory_sync_state,
            DirectorySyncState.NOT_ATTEMPTED,
        )
        self.assertFalse(raised.exception.replacement_completed)
        self.assertEqual(load_checkpoint(self.path), previous)
        self.assertEqual(len(list(self.path.parent.glob(".*.tmp"))), 1)

    def test_directory_fsync_failure_is_unconfirmed_and_keeps_target(self):
        self._write_with_directory_sync_mocked(self.checkpoint)
        newer = self.checkpoint_from_updates(
            next_byte_offset=40,
            last_ingestion_key=create_ingestion_key("generation-α", 27, 40),
        )
        cause = OSError(errno.EIO, "directory sync failed")
        with mock.patch.object(
            checkpoint_module,
            "_fsync_directory",
            side_effect=cause,
        ):
            with self.assertRaises(DurabilityError) as raised:
                write_checkpoint(self.path, newer)

        self.assertIsInstance(raised.exception, CheckpointWriteError)
        self.assertIs(raised.exception.original_cause, cause)
        self.assertTrue(raised.exception.published)
        self.assertTrue(raised.exception.replacement_completed)
        self.assertEqual(
            raised.exception.publication_state,
            PublicationState.PUBLISHED_UNCONFIRMED,
        )
        self.assertEqual(
            raised.exception.directory_sync_state,
            DirectorySyncState.FAILED,
        )
        self.assertFalse(raised.exception.outcome.is_durable_success)
        self.assertEqual(load_checkpoint(self.path), newer)

    def test_unsupported_directory_fsync_raises_unconfirmed_error(self):
        cause = NotImplementedError("directory fsync unsupported")
        with mock.patch.object(
            checkpoint_module,
            "_fsync_directory",
            side_effect=cause,
        ):
            with self.assertRaises(CheckpointWriteError) as raised:
                write_checkpoint(self.path, self.checkpoint)

        self.assertIs(raised.exception.original_cause, cause)
        self.assertTrue(raised.exception.published)
        self.assertEqual(
            raised.exception.publication_state,
            PublicationState.PUBLISHED_UNCONFIRMED,
        )
        self.assertEqual(
            raised.exception.directory_sync_state,
            DirectorySyncState.UNSUPPORTED,
        )
        self.assertFalse(raised.exception.outcome.is_durable_success)
        self.assertEqual(load_checkpoint(self.path), self.checkpoint)

    def test_unsupported_directory_fsync_errno_is_classified_as_unsupported(self):
        cause = OSError(errno.EINVAL, "directory fsync unsupported")
        with mock.patch.object(
            checkpoint_module,
            "_fsync_directory",
            side_effect=cause,
        ):
            with self.assertRaises(CheckpointWriteError) as raised:
                write_checkpoint(self.path, self.checkpoint)

        self.assertIs(raised.exception.original_cause, cause)
        self.assertEqual(
            raised.exception.directory_sync_state,
            DirectorySyncState.UNSUPPORTED,
        )
        self.assertEqual(load_checkpoint(self.path), self.checkpoint)

    def test_directory_fsync_success_is_reported(self):
        with mock.patch.object(checkpoint_module, "_fsync_directory"):
            result = write_checkpoint(self.path, self.checkpoint)
        self.assertTrue(result.directory_fsynced)
        self.assertIsNone(result.durability_note)
        self.assertTrue(result.outcome.is_durable_success)
        self.assertEqual(result.outcome.destination, self.path)

    def test_checkpoint_v2_schema_and_validation_contract(self):
        serialized = checkpoint_to_json(self.checkpoint)
        self.assertEqual(set(json.loads(serialized)), set(self.data()))
        self.assertEqual(json.loads(serialized)["version"], 2)
        self.assertEqual(checkpoint_from_json(serialized), self.checkpoint)
        with self.assertRaises(CheckpointValidationError):
            self.checkpoint_from_updates(next_byte_offset=-1)

    def test_missing_and_corrupt_checkpoints_are_distinguished(self):
        with self.assertRaises(CheckpointNotFoundError):
            load_checkpoint(self.path)

        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"\xff")
        with self.assertRaises(CheckpointCorruptionError):
            load_checkpoint(self.path)
        self.assertEqual(self.path.read_bytes(), b"\xff")

    def test_unsupported_version_is_corruption_and_never_resets_state(self):
        self.path.parent.mkdir(parents=True)
        invalid = {**self.data(), "version": 3}
        content = json.dumps(invalid).encode("utf-8")
        self.path.write_bytes(content)

        with self.assertRaises(CheckpointCorruptionError):
            load_checkpoint(self.path)

        self.assertEqual(self.path.read_bytes(), content)

    def test_checkpoint_module_does_not_depend_on_inbox_writer(self):
        self.assertNotIn("inbox_writer", checkpoint_module.__dict__)
        with self.assertRaises(TypeError):
            write_checkpoint(self.path)
        self.assertFalse(self.path.exists())

    def _write_with_directory_sync_mocked(self, checkpoint):
        with mock.patch.object(checkpoint_module, "_fsync_directory"):
            return write_checkpoint(self.path, checkpoint)


if __name__ == "__main__":
    unittest.main()
