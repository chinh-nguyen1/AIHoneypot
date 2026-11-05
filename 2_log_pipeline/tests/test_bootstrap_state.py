import hashlib
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


PIPELINE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE_DIR))

if "requests" not in sys.modules:
    requests_module = types.ModuleType("requests")
    request_exception = type("RequestException", (Exception,), {})
    requests_module.RequestException = request_exception
    requests_module.Timeout = type("Timeout", (request_exception,), {})
    requests_module.ConnectionError = type(
        "ConnectionError", (request_exception,), {}
    )
    requests_module.post = mock.Mock()
    requests_exceptions = types.ModuleType("requests.exceptions")
    requests_exceptions.JSONDecodeError = json.JSONDecodeError
    requests_module.exceptions = requests_exceptions
    sys.modules["requests"] = requests_module
    sys.modules["requests.exceptions"] = requests_exceptions

if "mysql.connector" not in sys.modules:
    mysql_module = types.ModuleType("mysql")
    mysql_module.__path__ = []
    connector_module = types.ModuleType("mysql.connector")
    connector_module.connect = mock.Mock()
    mysql_module.connector = connector_module
    sys.modules["mysql"] = mysql_module
    sys.modules["mysql.connector"] = connector_module

import checkpoint as checkpoint_module
import config
from bootstrap_state import (
    BootstrapError,
    PipelineStateStatus,
    bootstrap_state,
    inspect_pipeline_state,
    main as bootstrap_main,
)
from checkpoint import (
    Checkpoint,
    CheckpointWriteError,
    CHECKPOINT_VERSION,
    EMPTY_SOURCE_PREFIX_SHA256,
    create_checkpoint_if_absent,
    load_checkpoint,
    write_checkpoint,
)
from inbox_writer import write_inbox_event
from inbox_record import create_inbox_event


_HOLD_LOCK_SCRIPT = """
import sys
sys.path.insert(0, sys.argv[1])
from single_writer_lock import SingleWriterLock
with SingleWriterLock(
    sys.argv[2], checkpoint_path=sys.argv[3], inbox_directory=sys.argv[4]
):
    print("LOCKED", flush=True)
    sys.stdin.read(1)
"""

_CONCURRENT_BOOTSTRAP_SCRIPT = """
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import checkpoint
checkpoint._fsync_directory = lambda directory: None
import bootstrap_state

original_inspect = bootstrap_state.inspect_pipeline_state
inspection_count = 0
barrier = Path(sys.argv[10])

def synchronized_inspect(**kwargs):
    global inspection_count
    result = original_inspect(**kwargs)
    inspection_count += 1
    if inspection_count == 1:
        (barrier / f"ready-{os.getpid()}").touch()
        deadline = time.monotonic() + 15
        while len(list(barrier.glob("ready-*"))) < 2:
            if time.monotonic() >= deadline:
                raise RuntimeError("concurrent test barrier timed out")
            time.sleep(0.005)
    return result

bootstrap_state.inspect_pipeline_state = synchronized_inspect
try:
    result = bootstrap_state.bootstrap_state(
        sys.argv[2],
        source_path=sys.argv[3],
        checkpoint_path=sys.argv[4],
        inbox_directory=sys.argv[5],
        lock_path=sys.argv[6],
        consumer_state_directory=sys.argv[7],
        consumer_lock_path=sys.argv[8],
        rejected_file_path=sys.argv[9],
    )
except Exception as exc:
    print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
    raise SystemExit(2)
print(f"created={result.created}")
"""


class BootstrapStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.logs_directory = self.root / "logs"
        self.logs_directory.mkdir()
        self.state_directory = self.root / "state"
        self.state_directory.mkdir()
        self.source = self.logs_directory / "source.log"
        self.source.write_bytes(b"")
        self.checkpoint = self.state_directory / "checkpoint.json"
        self.inbox = self.state_directory / "inbox"
        self.lock_path = self.state_directory / "single-writer.lock"
        self.enterContext(
            mock.patch.object(checkpoint_module, "_fsync_directory")
        )

    def bootstrap(self, mode):
        return bootstrap_state(
            mode,
            source_path=self.source,
            checkpoint_path=self.checkpoint,
            inbox_directory=self.inbox,
            lock_path=self.lock_path,
            consumer_state_directory=self.state_directory / "consumer",
            consumer_lock_path=self.state_directory / "consumer.lock",
            rejected_file_path=self.state_directory / "rejected.jsonl",
        )

    def inspect(self):
        return inspect_pipeline_state(
            source_path=self.source,
            checkpoint_path=self.checkpoint,
            inbox_directory=self.inbox,
            consumer_state_directory=self.state_directory / "consumer",
            lock_path=self.lock_path,
            consumer_lock_path=self.state_directory / "consumer.lock",
            rejected_file_path=self.state_directory / "rejected.jsonl",
        )

    def prepare_checkpoint(self, offset=0):
        source_stat = self.source.stat()
        identity = f"stat-v1:{source_stat.st_dev}:{source_stat.st_ino}"
        checkpoint = Checkpoint(
            version=CHECKPOINT_VERSION,
            source_path=str(self.source.resolve()),
            source_file_identity=identity,
            next_byte_offset=offset,
            last_ingestion_key=None,
            updated_at="2026-11-01T04:00:00+00:00",
            source_prefix_sha256=hashlib.sha256(
                self.source.read_bytes()[:offset]
            ).hexdigest(),
        )
        with mock.patch.object(checkpoint_module, "_fsync_directory"):
            write_checkpoint(self.checkpoint, checkpoint)
        return checkpoint

    def new_checkpoint_value(self, offset=0):
        source_stat = self.source.stat()
        return Checkpoint(
            version=CHECKPOINT_VERSION,
            source_path=str(self.source.resolve()),
            source_file_identity=(
                f"stat-v1:{source_stat.st_dev}:{source_stat.st_ino}"
            ),
            next_byte_offset=offset,
            last_ingestion_key=None,
            updated_at="2026-11-01T04:00:00+00:00",
            source_prefix_sha256=hashlib.sha256(
                self.source.read_bytes()[:offset]
            ).hexdigest(),
        )

    def test_from_start_creates_checkpoint_at_zero_without_reading_records(self):
        self.source.write_bytes(b"not parsed by bootstrap\n")

        result = self.bootstrap("from-start")

        self.assertTrue(result.created)
        self.assertEqual(result.checkpoint.next_byte_offset, 0)
        self.assertEqual(
            result.checkpoint.source_prefix_sha256,
            EMPTY_SOURCE_PREFIX_SHA256,
        )
        self.assertEqual(load_checkpoint(self.checkpoint), result.checkpoint)
        self.assertEqual(list(self.inbox.iterdir()), [])

    def test_from_end_creates_checkpoint_at_file_size_after_lf(self):
        self.source.write_bytes(b"opaque contents\n")

        result = self.bootstrap("from-end")

        self.assertTrue(result.created)
        self.assertEqual(result.checkpoint.next_byte_offset, len(b"opaque contents\n"))
        self.assertEqual(
            result.checkpoint.source_prefix_sha256,
            hashlib.sha256(b"opaque contents\n").hexdigest(),
        )

    def test_from_end_accepts_empty_source(self):
        result = self.bootstrap("from-end")

        self.assertTrue(result.created)
        self.assertEqual(result.checkpoint.next_byte_offset, 0)

    def test_existing_checkpoint_is_not_overwritten(self):
        self.inbox.mkdir()
        existing = self.prepare_checkpoint()
        original_bytes = self.checkpoint.read_bytes()

        with self.assertRaisesRegex(BootstrapError, "requires UNINITIALIZED"):
            self.bootstrap("from-start")

        self.assertIsNotNone(existing)
        self.assertEqual(self.checkpoint.read_bytes(), original_bytes)

    def test_exclusive_checkpoint_creation_does_not_replace_existing_file(self):
        self.prepare_checkpoint()
        original_bytes = self.checkpoint.read_bytes()

        with self.assertRaises(CheckpointWriteError):
            create_checkpoint_if_absent(
                self.checkpoint,
                self.new_checkpoint_value(),
            )

        self.assertEqual(self.checkpoint.read_bytes(), original_bytes)

    def test_corrupt_checkpoint_fails_without_replacement(self):
        original_bytes = b"{broken checkpoint"
        self.checkpoint.write_bytes(original_bytes)

        with self.assertRaisesRegex(BootstrapError, "CORRUPT"):
            self.bootstrap("from-start")

        self.assertEqual(self.checkpoint.read_bytes(), original_bytes)

    def test_existing_checkpoint_with_source_identity_mismatch_fails_closed(self):
        original = self.prepare_checkpoint()
        invalid = Checkpoint(
            version=original.version,
            source_path=original.source_path,
            source_file_identity="different-generation",
            next_byte_offset=0,
            last_ingestion_key=None,
            updated_at=original.updated_at,
            source_prefix_sha256=EMPTY_SOURCE_PREFIX_SHA256,
        )
        with mock.patch.object(checkpoint_module, "_fsync_directory"):
            write_checkpoint(self.checkpoint, invalid)
        original_bytes = self.checkpoint.read_bytes()

        with self.assertRaisesRegex(BootstrapError, "requires UNINITIALIZED"):
            self.bootstrap("from-start")

        self.assertEqual(self.checkpoint.read_bytes(), original_bytes)

    def test_inbox_event_without_checkpoint_fails_closed(self):
        self.inbox.mkdir()
        raw_record = b"fixture\n"
        event = create_inbox_event("fixture-generation", 0, len(raw_record), raw_record)
        with mock.patch("inbox_writer._fsync_directory"):
            write_inbox_event(self.inbox, event)
        inbox_entries = sorted(path.name for path in self.inbox.iterdir())

        with self.assertRaisesRegex(BootstrapError, "INCONSISTENT"):
            self.bootstrap("from-start")

        self.assertFalse(self.checkpoint.exists())
        self.assertEqual(
            sorted(path.name for path in self.inbox.iterdir()),
            inbox_entries,
        )

    def test_from_end_rejects_non_lf_ending_without_checkpoint(self):
        self.source.write_bytes(b"opaque contents")

        with self.assertRaisesRegex(BootstrapError, "prefix to end in LF"):
            self.bootstrap("from-end")

        self.assertFalse(self.checkpoint.exists())
        self.assertFalse(self.inbox.exists())

    def test_other_process_holding_lock_prevents_bootstrap(self):
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _HOLD_LOCK_SCRIPT,
                str(PIPELINE_DIR),
                str(self.lock_path),
                str(self.checkpoint),
                str(self.inbox),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "LOCKED")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(PIPELINE_DIR / "bootstrap_state.py"),
                    "from-start",
                    "--source",
                    str(self.source),
                    "--checkpoint",
                    str(self.checkpoint),
                    "--inbox",
                    str(self.inbox),
                    "--lock",
                    str(self.lock_path),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            self.assertEqual(completed.returncode, 2, completed.stderr)
            self.assertIn("watcher lock: present", completed.stderr)
            self.assertFalse(self.checkpoint.exists())
        finally:
            if process.stdin:
                process.stdin.write("x")
                process.stdin.flush()
            process.wait(timeout=15)
            if process.returncode:
                self.fail(process.stderr.read())

    def test_existing_empty_inbox_is_not_reused_or_modified(self):
        self.inbox.mkdir()
        original_entries = sorted(path.name for path in self.inbox.iterdir())

        with self.assertRaisesRegex(BootstrapError, "INCONSISTENT"):
            self.bootstrap("from-start")

        self.assertEqual(
            sorted(path.name for path in self.inbox.iterdir()),
            original_entries,
        )
        self.assertFalse(self.checkpoint.exists())
        self.assertFalse(self.lock_path.exists())

    def test_existing_consumer_state_directory_blocks_bootstrap(self):
        consumer_state = self.state_directory / "consumer"
        consumer_state.mkdir()

        with self.assertRaisesRegex(BootstrapError, "INCONSISTENT"):
            self.bootstrap("from-start")

        self.assertEqual(list(consumer_state.iterdir()), [])
        self.assertFalse(self.inbox.exists())
        self.assertFalse(self.checkpoint.exists())
        self.assertFalse(self.lock_path.exists())

    def test_retry_rejected_and_processed_sidecars_block_bootstrap(self):
        consumer_state = self.state_directory / "consumer"
        consumer_state.mkdir()
        sidecar_specs = (
            ("RETRY", 1, "temporary", "2026-11-03T00:01:00+00:00"),
            ("REJECTED", 3, "permanent", None),
            ("PROCESSED", 0, None, None),
        )
        for index, (status, retry_count, reason, next_attempt_at) in enumerate(
            sidecar_specs
        ):
            ingestion_key = f'["fixture-generation",{index},1]'
            sidecar = {
                "version": 1,
                "ingestion_key": ingestion_key,
                "status": status,
                "retry_count": retry_count,
                "updated_at": "2026-11-03T00:00:00+00:00",
                "reason": reason,
                "next_attempt_at": next_attempt_at,
            }
            filename = hashlib.sha256(
                ingestion_key.encode("utf-8")
            ).hexdigest() + ".json"
            (consumer_state / filename).write_text(
                json.dumps(sidecar),
                encoding="utf-8",
            )
        original_contents = {
            entry.name: entry.read_bytes() for entry in consumer_state.iterdir()
        }

        with self.assertRaisesRegex(BootstrapError, "INCONSISTENT"):
            self.bootstrap("from-start")

        self.assertEqual(
            {entry.name: entry.read_bytes() for entry in consumer_state.iterdir()},
            original_contents,
        )
        self.assertFalse(self.inbox.exists())
        self.assertFalse(self.checkpoint.exists())
        self.assertFalse(self.lock_path.exists())

    def test_unknown_and_temporary_state_files_block_bootstrap(self):
        unknown = self.state_directory / "unexpected.bin"
        temporary = self.state_directory / ".checkpoint.json.partial.tmp"
        unknown.write_bytes(b"keep")
        temporary.write_bytes(b"keep")

        with self.assertRaisesRegex(BootstrapError, "INCONSISTENT"):
            self.bootstrap("from-start")

        self.assertEqual(unknown.read_bytes(), b"keep")
        self.assertEqual(temporary.read_bytes(), b"keep")
        self.assertFalse(self.inbox.exists())
        self.assertFalse(self.checkpoint.exists())
        self.assertFalse(self.lock_path.exists())

    def test_failed_checkpoint_creation_leaves_inbox_for_manual_recovery(self):
        failure = OSError("injected checkpoint creation failure")
        with mock.patch(
            "bootstrap_state.create_checkpoint_if_absent",
            side_effect=failure,
        ):
            with self.assertRaisesRegex(OSError, "injected checkpoint"):
                self.bootstrap("from-start")

        self.assertTrue(self.inbox.is_dir())
        self.assertEqual(list(self.inbox.iterdir()), [])
        self.assertFalse(self.checkpoint.exists())
        with self.assertRaisesRegex(BootstrapError, "INCONSISTENT"):
            self.bootstrap("from-start")
        self.assertTrue(self.inbox.is_dir())
        self.assertFalse(self.checkpoint.exists())

    def test_partial_checkpoint_write_is_preserved_and_blocks_retry(self):
        def partial_write(descriptor, content):
            del content
            os.write(descriptor, b'{"partial":')
            raise OSError("injected partial write")

        with mock.patch.object(
            checkpoint_module,
            "_write_temp_file",
            side_effect=partial_write,
        ):
            with self.assertRaises(CheckpointWriteError):
                create_checkpoint_if_absent(
                    self.checkpoint,
                    self.new_checkpoint_value(),
                )

        self.assertEqual(self.checkpoint.read_bytes(), b'{"partial":')
        with self.assertRaisesRegex(BootstrapError, "CORRUPT"):
            self.bootstrap("from-start")
        self.assertEqual(self.checkpoint.read_bytes(), b'{"partial":')
        self.assertFalse(self.inbox.exists())

    def test_two_concurrent_bootstraps_allow_only_one_creator(self):
        barrier = self.root / "barrier"
        barrier.mkdir()
        arguments = [
            str(PIPELINE_DIR),
            "from-start",
            str(self.source),
            str(self.checkpoint),
            str(self.inbox),
            str(self.lock_path),
            str(self.state_directory / "consumer"),
            str(self.state_directory / "consumer.lock"),
            str(self.state_directory / "rejected.jsonl"),
            str(barrier),
        ]
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", _CONCURRENT_BOOTSTRAP_SCRIPT, *arguments],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        results = [process.communicate(timeout=30) for process in processes]

        self.assertEqual(sorted(process.returncode for process in processes), [0, 2])
        self.assertEqual(
            sum("created=True" in stdout for stdout, _ in results),
            1,
        )
        self.assertTrue(load_checkpoint(self.checkpoint))
        self.assertTrue(self.inbox.is_dir())

    def test_inspect_reports_uninitialized_without_creating_state(self):
        before = sorted(path.name for path in self.state_directory.iterdir())

        result = self.inspect()

        self.assertEqual(result.status, PipelineStateStatus.UNINITIALIZED)
        self.assertIn("no checkpoint", " ".join(result.details))
        self.assertEqual(
            sorted(path.name for path in self.state_directory.iterdir()),
            before,
        )
        self.assertFalse(self.lock_path.exists())
        self.assertFalse(self.inbox.exists())

    def test_existing_lock_is_reported_read_only_and_blocks_bootstrap(self):
        self.lock_path.write_bytes(b"persistent lock placeholder")
        original_lock = self.lock_path.read_bytes()

        inspection = self.inspect()

        self.assertEqual(inspection.status, PipelineStateStatus.UNINITIALIZED)
        self.assertIn("watcher lock: present", " ".join(inspection.details))
        with self.assertRaisesRegex(BootstrapError, "INCONSISTENT"):
            self.bootstrap("from-start")

        self.assertEqual(self.lock_path.read_bytes(), original_lock)
        self.assertFalse(self.inbox.exists())
        self.assertFalse(self.checkpoint.exists())

    def test_inspect_reports_legacy_valid_checkpoint_and_inbox(self):
        self.inbox.mkdir()
        self.prepare_checkpoint()

        result = self.inspect()

        self.assertEqual(result.status, PipelineStateStatus.LEGACY_VALID)
        self.assertIn("checkpoint: valid", " ".join(result.details))

    def test_inspect_reports_v2_and_counts_all_consumer_sidecar_statuses(self):
        self.inbox.mkdir()
        self.prepare_checkpoint()
        consumer_state = self.state_directory / "consumer"
        consumer_state.mkdir()
        states = (
            ("RETRY", 1, "temporary failure", "2026-11-03T00:01:00+00:00"),
            ("REJECTED", 3, "permanent failure", None),
            ("PROCESSED", 0, None, None),
        )
        for index, (status, retry_count, reason, next_attempt_at) in enumerate(
            states
        ):
            ingestion_key = f'["fixture-generation",{index},1]'
            sidecar = {
                "version": 1,
                "ingestion_key": ingestion_key,
                "status": status,
                "retry_count": retry_count,
                "updated_at": "2026-11-03T00:00:00+00:00",
                "reason": reason,
                "next_attempt_at": next_attempt_at,
            }
            sidecar_name = hashlib.sha256(
                ingestion_key.encode("utf-8")
            ).hexdigest() + ".json"
            (consumer_state / sidecar_name).write_text(
                json.dumps(sidecar),
                encoding="utf-8",
            )

        result = self.inspect()

        self.assertEqual(result.status, PipelineStateStatus.BOOTSTRAPPED_V2)
        details = " ".join(result.details)
        self.assertIn("RETRY=1", details)
        self.assertIn("REJECTED=1", details)
        self.assertIn("PROCESSED=1", " ".join(result.details))

    def test_inspect_classifies_invalid_consumer_sidecar_as_corrupt(self):
        self.inbox.mkdir()
        self.prepare_checkpoint()
        consumer_state = self.state_directory / "consumer"
        consumer_state.mkdir()
        ingestion_key = '["fixture-generation",0,1]'
        sidecar_name = hashlib.sha256(
            ingestion_key.encode("utf-8")
        ).hexdigest() + ".json"
        (consumer_state / sidecar_name).write_text(
            json.dumps(
                {
                    "version": 1,
                    "ingestion_key": ingestion_key,
                    "status": "RETRY",
                    "retry_count": 1,
                    "updated_at": "2026-11-03T00:00:00+00:00",
                    "reason": "temporary failure",
                    "next_attempt_at": None,
                }
            ),
            encoding="utf-8",
        )

        result = self.inspect()

        self.assertEqual(result.status, PipelineStateStatus.CORRUPT)
        self.assertIn("ConsumerIntegrityError", " ".join(result.details))

    def test_inspect_reports_checkpoint_without_inbox_as_inconsistent(self):
        self.prepare_checkpoint()

        result = self.inspect()

        self.assertEqual(result.status, PipelineStateStatus.INCONSISTENT)
        self.assertIn("Inbox", " ".join(result.details))

    def test_inspect_does_not_read_inbox_event_contents(self):
        self.inbox.mkdir()
        event_path = self.inbox / (("a" * 64) + ".event.json")
        event_path.write_bytes(b"payload must not be read")
        event_stat_before = event_path.stat()
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path in (event_path, self.source):
                raise AssertionError("inspect must not read Inbox or source content")
            return original_open(path, *args, **kwargs)

        with mock.patch.object(
            Path,
            "read_bytes",
            side_effect=AssertionError("inspect must not read Inbox event contents"),
        ), mock.patch.object(Path, "open", guarded_open):
            result = self.inspect()

        self.assertEqual(result.status, PipelineStateStatus.INCONSISTENT)
        self.assertIn(
            "persisted components exist without a checkpoint",
            " ".join(result.details),
        )
        self.assertNotIn("payload must not be read", " ".join(result.details))
        event_stat_after = event_path.stat()
        self.assertEqual(event_stat_after.st_size, event_stat_before.st_size)
        self.assertEqual(event_stat_after.st_mtime_ns, event_stat_before.st_mtime_ns)

    def test_inspect_reports_unknown_and_temporary_components(self):
        self.state_directory.joinpath("unexpected.bin").write_bytes(b"x")
        (self.state_directory / ".checkpoint.json.partial.tmp").write_bytes(b"x")

        result = self.inspect()

        self.assertEqual(result.status, PipelineStateStatus.INCONSISTENT)
        self.assertIn("unexpected.bin", " ".join(result.details))
        self.assertIn("temporary file", " ".join(result.details))

    def test_inspect_reports_corrupt_checkpoint_without_changing_it(self):
        self.checkpoint.write_bytes(b"{invalid")
        original = self.checkpoint.read_bytes()

        result = self.inspect()

        self.assertEqual(result.status, PipelineStateStatus.CORRUPT)
        self.assertEqual(self.checkpoint.read_bytes(), original)

    def test_inspect_reports_source_mismatch_using_metadata_only(self):
        self.inbox.mkdir()
        self.prepare_checkpoint()
        other_source = self.logs_directory / "other.log"
        other_source.write_bytes(b"")

        result = inspect_pipeline_state(
            source_path=other_source,
            checkpoint_path=self.checkpoint,
            inbox_directory=self.inbox,
            consumer_state_directory=self.state_directory / "consumer",
            lock_path=self.lock_path,
            consumer_lock_path=self.state_directory / "consumer.lock",
            rejected_file_path=self.state_directory / "rejected.jsonl",
        )

        self.assertEqual(result.status, PipelineStateStatus.SOURCE_MISMATCH)

    def test_inspect_cli_requires_no_bootstrap_mode_and_does_not_create_lock(self):
        output = mock.Mock()
        with mock.patch("builtins.print", output):
            result = bootstrap_main(
                [
                    "inspect",
                    "--source",
                    str(self.source),
                    "--checkpoint",
                    str(self.checkpoint),
                    "--inbox",
                    str(self.inbox),
                    "--lock",
                    str(self.lock_path),
                    "--consumer-state",
                    str(self.state_directory / "consumer"),
                    "--consumer-lock",
                    str(self.state_directory / "consumer.lock"),
                    "--rejected-file",
                    str(self.state_directory / "rejected.jsonl"),
                ]
            )

        self.assertEqual(result, 0)
        output.assert_any_call("Pipeline state: UNINITIALIZED")
        self.assertFalse(self.lock_path.exists())


class MySQLConfigurationTests(unittest.TestCase):
    def test_missing_password_fails_with_clear_configuration_error(self):
        with self.assertRaisesRegex(ValueError, "MYSQL_PASSWORD is required"):
            config.get_mysql_config({})

    def test_pipeline_uses_matching_non_root_application_credentials(self):
        settings = {
            "MYSQL_HOST": "database",
            "MYSQL_PORT": "3306",
            "MYSQL_USER": "honeypot_pipeline",
            "MYSQL_PASSWORD": "test-only-password",
            "MYSQL_DATABASE": "ai_webhoneypot_db",
        }

        result = config.get_mysql_config(settings)

        self.assertEqual(
            result,
            {
                "host": "database",
                "port": 3306,
                "user": "honeypot_pipeline",
                "password": "test-only-password",
                "database": "ai_webhoneypot_db",
            },
        )

    def test_pipeline_rejects_root_account(self):
        settings = {
            "MYSQL_USER": "root",
            "MYSQL_PASSWORD": "test-only-password",
        }

        with self.assertRaisesRegex(ValueError, "non-root"):
            config.get_mysql_config(settings)

    def test_compose_uses_same_app_environment_for_database_and_pipeline(self):
        compose_path = Path(__file__).resolve().parents[2] / "docker-compose.yml"
        compose = compose_path.read_text(encoding="utf-8")
        database_section = compose.split("  database:\n", 1)[1].split(
            "  decoy_web:\n", 1
        )[0]
        pipeline_section = compose.split("  log_pipeline:\n", 1)[1].split(
            "  frontend_dashboard:\n", 1
        )[0]

        for variable in ("MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE"):
            with self.subTest(variable=variable):
                self.assertIn(f"{variable}:", database_section)
                self.assertIn(f"{variable}:", pipeline_section)
        self.assertIn("MYSQL_ROOT_PASSWORD:", database_section)
        self.assertNotIn("MYSQL_ROOT_PASSWORD:", pipeline_section)
        self.assertIn("MYSQL_HOST:", pipeline_section)
        self.assertIn("MYSQL_PORT:", pipeline_section)
        ai_section = compose.split("  ai_engine:\n", 1)[1].split(
            "  log_pipeline:\n", 1
        )[0]
        for variable in (
            "MYSQL_HOST",
            "MYSQL_PORT",
            "MYSQL_USER",
            "MYSQL_PASSWORD",
            "MYSQL_DATABASE",
        ):
            with self.subTest(ai_variable=variable):
                self.assertIn(f"{variable}:", ai_section)
        self.assertNotIn("MYSQL_ROOT_PASSWORD:", ai_section)
        self.assertIn("--port=${MYSQL_PORT:-3306}", database_section)
        database_dockerfile = (
            Path(__file__).resolve().parents[2] / "Dockerfile.database"
        ).read_text(encoding="utf-8")
        self.assertNotIn("MYSQL_ROOT_PASSWORD=", database_dockerfile)
        self.assertNotIn("MYSQL_PASSWORD=", database_dockerfile)


if __name__ == "__main__":
    unittest.main()
