"""Streaming normalization and deduplication for imported dataset batches."""

import hashlib
import json
import sqlite3
import tempfile
import unicodedata
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Optional, TextIO, Union

from data_import import DatasetRecord, SUPPORTED_LABELS, normalize_label


PathLike = Union[str, Path]


class DatasetNormalizationError(ValueError):
    """Raised when normalization reports cannot be safely created."""


@dataclass(frozen=True)
class NormalizedDatasetRecord:
    """A retained record; payload is unchanged from the importer input."""

    payload: str
    label: str
    source_file: str
    source_record: int


@dataclass
class NormalizationStats:
    """Counters finalized as the lazy output iterator is fully consumed."""

    total_records: int = 0
    valid_records: int = 0
    rejected_records: int = 0
    duplicate_records: int = 0
    conflicting_records: int = 0
    unique_records: int = 0
    counts_by_label: dict[str, int] = field(
        default_factory=lambda: {label: 0 for label in SUPPORTED_LABELS}
    )
    counts_by_reject_reason: dict[str, int] = field(default_factory=dict)


class _NullTextWriter:
    def __enter__(self) -> "_NullTextWriter":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        return False

    def write(self, value: str) -> int:
        return len(value)


class DatasetNormalizer:
    """Normalize importer batches while keeping duplicate state on disk.

    Input must be an iterable of batches containing ``DatasetRecord`` values
    from ``data_import.DatasetImporter.iter_batches()``. Mapping records with
    the same four fields are also accepted. Output is a lazy iterator of
    ``NormalizedDatasetRecord`` batches. Same-label duplicates keep the first
    source record; any payload group with multiple labels is excluded entirely.

    ``valid_records`` counts records passing field validation, including
    duplicates and later-conflicting records. ``duplicate_records`` counts
    valid occurrences after the first occurrence of a normalized payload.
    ``conflicting_records`` counts every valid record in a multi-label group.
    ``unique_records`` and ``counts_by_label`` describe the final output.
    Statistics are complete only after the output iterator is exhausted.
    """

    def __init__(
        self,
        *,
        batch_size: int = 1000,
        reject_report_path: Optional[PathLike] = None,
        conflict_report_path: Optional[PathLike] = None,
    ) -> None:
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        self.batch_size = batch_size
        self.reject_report_path = (
            Path(reject_report_path) if reject_report_path is not None else None
        )
        self.conflict_report_path = (
            Path(conflict_report_path)
            if conflict_report_path is not None
            else None
        )
        if (
            self.reject_report_path is not None
            and self.conflict_report_path is not None
            and self.reject_report_path.resolve()
            == self.conflict_report_path.resolve()
        ):
            raise ValueError("reject and conflict reports must use different paths")
        self.stats = NormalizationStats()

    def iter_batches(
        self, imported_batches: Iterable[Iterable[object]]
    ) -> Iterator[list[NormalizedDatasetRecord]]:
        """Consume importer batches and yield bounded batches of final records."""
        self.stats = NormalizationStats()
        batch: list[NormalizedDatasetRecord] = []

        with tempfile.TemporaryDirectory(
            prefix="aihoneypot-dataset-normalization-"
        ) as temp_directory:
            database = sqlite3.connect(
                Path(temp_directory) / "normalized-payloads.sqlite3"
            )
            try:
                database.execute(
                    "CREATE TABLE payload_groups ("
                    "payload_key TEXT PRIMARY KEY, "
                    "payload TEXT NOT NULL, "
                    "label TEXT NOT NULL, "
                    "source_file TEXT NOT NULL, "
                    "source_record INTEGER NOT NULL, "
                    "conflicted INTEGER NOT NULL DEFAULT 0, "
                    "occurrences INTEGER NOT NULL DEFAULT 1)"
                )
                database.execute(
                    "CREATE TABLE occurrences ("
                    "payload_key TEXT NOT NULL, "
                    "label TEXT NOT NULL, "
                    "source_file TEXT NOT NULL, "
                    "source_record INTEGER NOT NULL, "
                    "sequence INTEGER PRIMARY KEY AUTOINCREMENT)"
                )
                with ExitStack() as stack:
                    reject_report = stack.enter_context(
                        self._open_report(self.reject_report_path, "reject")
                    )
                    conflict_report = stack.enter_context(
                        self._open_report(self.conflict_report_path, "conflict")
                    )

                    for imported_batch in imported_batches:
                        try:
                            records = iter(imported_batch)
                        except TypeError:
                            self._reject(
                                None,
                                None,
                                "batch is not iterable",
                                reject_report,
                            )
                            continue

                        for record in records:
                            self.stats.total_records += 1
                            values, reason = self._extract_record(record)
                            if reason is not None:
                                source_file, source_record = self._source_fields(
                                    record
                                )
                                self._reject(
                                    source_file,
                                    source_record,
                                    reason,
                                    reject_report,
                                )
                                continue

                            payload, label, source_file, source_record = values
                            normalized_payload = unicodedata.normalize("NFC", payload)
                            try:
                                payload_bytes = normalized_payload.encode("utf-8")
                            except UnicodeEncodeError:
                                self._reject(
                                    source_file,
                                    source_record,
                                    "payload contains invalid Unicode",
                                    reject_report,
                                )
                                continue

                            self._record_valid(
                                database,
                                normalized_payload,
                                payload,
                                label,
                                source_file,
                                source_record,
                            )

                    self._write_conflicts(database, conflict_report)

                    cursor = database.execute(
                        "SELECT payload, label, source_file, source_record "
                        "FROM payload_groups WHERE conflicted = 0 "
                        "ORDER BY rowid"
                    )
                    for payload, label, source_file, source_record in cursor:
                        batch.append(
                            NormalizedDatasetRecord(
                                payload=payload,
                                label=label,
                                source_file=source_file,
                                source_record=source_record,
                            )
                        )
                        self.stats.unique_records += 1
                        self.stats.counts_by_label[label] += 1
                        if len(batch) == self.batch_size:
                            yield batch
                            batch = []

                    if batch:
                        yield batch
            finally:
                database.close()

    def _open_report(self, path: Optional[Path], report_type: str):
        if path is None:
            return _NullTextWriter()
        try:
            return path.open("x", encoding="utf-8", newline="\n")
        except OSError as exc:
            raise DatasetNormalizationError(
                f"{report_type.capitalize()} report could not be created: {path}"
            ) from exc

    @staticmethod
    def _source_fields(record: object) -> tuple[Optional[str], Optional[int]]:
        if isinstance(record, Mapping):
            source_file = record.get("source_file")
            source_record = record.get("source_record")
        elif isinstance(record, DatasetRecord):
            source_file = record.source_file
            source_record = record.source_record
        else:
            return None, None

        if not isinstance(source_file, str) or not source_file:
            source_file = None
        if type(source_record) is not int or source_record < 1:
            source_record = None
        return source_file, source_record

    @classmethod
    def _extract_record(
        cls, record: object
    ) -> tuple[
        tuple[str, str, str, int],
        Optional[str],
    ]:
        source_file, source_record = cls._source_fields(record)
        if isinstance(record, Mapping):
            payload = record.get("payload")
            label_value = record.get("label")
        elif isinstance(record, DatasetRecord):
            payload = record.payload
            label_value = record.label
        else:
            return (("", "", "", 0), "record must be a DatasetRecord or mapping")

        if source_file is None or source_record is None:
            return (("", "", "", 0), "source_file/source_record is invalid")
        if not isinstance(payload, str):
            return (("", "", source_file, source_record), "payload is missing or is not a string")
        if not payload.strip():
            return (("", "", source_file, source_record), "payload is empty")
        if not isinstance(label_value, str) or not label_value.strip():
            return (("", "", source_file, source_record), "label is missing")

        label = normalize_label(label_value)
        if label is None:
            return (
                ("", "", source_file, source_record),
                "label is not one of Normal, SQLi, XSS or a declared alias",
            )
        return ((payload, label, source_file, source_record), None)

    def _record_valid(
        self,
        database: sqlite3.Connection,
        normalized_payload: str,
        payload: str,
        label: str,
        source_file: str,
        source_record: int,
    ) -> None:
        existing = database.execute(
            "SELECT label FROM payload_groups WHERE payload_key = ?",
            (normalized_payload,),
        ).fetchone()
        if existing is None:
            database.execute(
                "INSERT INTO payload_groups "
                "(payload_key, payload, label, source_file, source_record) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    normalized_payload,
                    payload,
                    label,
                    source_file,
                    source_record,
                ),
            )
        else:
            self.stats.duplicate_records += 1
            if existing[0] != label:
                database.execute(
                    "UPDATE payload_groups SET conflicted = 1 "
                    "WHERE payload_key = ?",
                    (normalized_payload,),
                )
            database.execute(
                "UPDATE payload_groups SET occurrences = occurrences + 1 "
                "WHERE payload_key = ?",
                (normalized_payload,),
            )

        database.execute(
            "INSERT INTO occurrences "
            "(payload_key, label, source_file, source_record) "
            "VALUES (?, ?, ?, ?)",
            (normalized_payload, label, source_file, source_record),
        )
        self.stats.valid_records += 1

    def _write_conflicts(
        self, database: sqlite3.Connection, report: TextIO
    ) -> None:
        database.execute(
            """
            UPDATE payload_groups
            SET conflicted = 1
            WHERE payload_key IN (
                SELECT payload_key
                FROM occurrences
                GROUP BY payload_key
                HAVING COUNT(DISTINCT label) > 1
            )
            """
        )
        self.stats.conflicting_records = database.execute(
            """
            SELECT COALESCE(SUM(occurrences), 0)
            FROM payload_groups
            WHERE conflicted = 1
            """
        ).fetchone()[0]

        rows = database.execute(
            """
            SELECT o.payload_key, o.label, o.source_file, o.source_record
            FROM occurrences o
            JOIN payload_groups g ON g.payload_key = o.payload_key
            WHERE g.conflicted = 1
            ORDER BY o.sequence
            """
        )
        for normalized_payload, label, source_file, source_record in rows:
            self._write_json_line(
                report,
                {
                    "payload_fingerprint": hashlib.sha256(
                        normalized_payload.encode("utf-8")
                    ).hexdigest(),
                    "label": label,
                    "source_file": source_file,
                    "source_record": source_record,
                },
            )

    def _reject(
        self,
        source_file: Optional[str],
        source_record: Optional[int],
        reason: str,
        report: TextIO,
    ) -> None:
        self.stats.rejected_records += 1
        self.stats.counts_by_reject_reason[reason] = (
            self.stats.counts_by_reject_reason.get(reason, 0) + 1
        )
        self._write_json_line(
            report,
            {
                "source_file": source_file,
                "source_record": source_record,
                "reason": reason,
            },
        )

    @staticmethod
    def _write_json_line(report: TextIO, value: dict[str, object]) -> None:
        json.dump(value, report, ensure_ascii=False, separators=(",", ":"))
        report.write("\n")


def normalize_batches(
    imported_batches: Iterable[Iterable[object]],
    *,
    batch_size: int = 1000,
    reject_report_path: Optional[PathLike] = None,
    conflict_report_path: Optional[PathLike] = None,
) -> tuple[Iterator[list[NormalizedDatasetRecord]], DatasetNormalizer]:
    """Create a normalizer and return its lazy output iterator and stats owner."""
    normalizer = DatasetNormalizer(
        batch_size=batch_size,
        reject_report_path=reject_report_path,
        conflict_report_path=conflict_report_path,
    )
    return normalizer.iter_batches(imported_batches), normalizer
