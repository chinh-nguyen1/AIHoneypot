"""Streaming import and validation for labeled external training datasets."""

import csv
import hashlib
import json
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional, TextIO, Union


SUPPORTED_LABELS = ("SQLi", "XSS", "Normal")
LABEL_ALIASES = {
    "normal": "Normal",
    "sqli": "SQLi",
    "sql_injection": "SQLi",
    "xss": "XSS",
    "cross_site_scripting": "XSS",
}
PathLike = Union[str, Path]


class DatasetImportError(ValueError):
    """Raised when an input file cannot be safely interpreted as a dataset."""


class UnsupportedDatasetFormatError(DatasetImportError):
    """Raised when a dataset format has no streaming reader."""


@dataclass(frozen=True)
class DatasetRecord:
    payload: str
    label: str
    source_file: str
    source_record: int


@dataclass
class ImportStats:
    records_read: int = 0
    valid_records: int = 0
    rejected_records: int = 0
    label_counts: dict[str, int] = field(
        default_factory=lambda: {label: 0 for label in SUPPORTED_LABELS}
    )
    empty_payloads: int = 0
    duplicate_payloads: int = 0


@dataclass(frozen=True)
class _Candidate:
    source_file: str
    source_record: int
    payload: object = None
    label: object = None
    error: Optional[str] = None


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def normalize_label(value: object) -> Optional[str]:
    """Return a canonical label for a declared alias, or None if unsupported."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
    return LABEL_ALIASES.get(normalized)


def _coerce_paths(paths: Union[PathLike, Iterable[PathLike]]) -> tuple[Path, ...]:
    if isinstance(paths, (str, Path)):
        return (Path(paths),)
    try:
        result = tuple(Path(path) for path in paths)
    except TypeError as exc:
        raise TypeError("paths must be a path or an iterable of paths") from exc
    if not result:
        raise ValueError("at least one dataset path is required")
    return result


class DatasetImporter:
    """Read CSV, JSONL, or tab-delimited TXT datasets without loading files whole.

    CSV uses a header row with configurable payload and label column names.
    JSONL requires one object per non-empty line with ``payload`` and ``label``.
    TXT requires one record per non-empty line in ``LABEL<TAB>PAYLOAD`` format.
    JSON arrays are intentionally unsupported because no streaming JSON parser
    is included in the current project dependencies.

    Exact duplicate payloads are counted but retained in the output. A temporary
    SQLite database stores seen payloads on disk so the duplicate index does not
    consume memory proportional to dataset size.
    """

    def __init__(
        self,
        paths: Union[PathLike, Iterable[PathLike]],
        *,
        batch_size: int = 1000,
        payload_column: str = "payload",
        label_column: str = "label",
        reject_report_path: Optional[PathLike] = None,
    ) -> None:
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        if not isinstance(payload_column, str) or not payload_column.strip():
            raise ValueError("payload_column must be a non-empty string")
        if not isinstance(label_column, str) or not label_column.strip():
            raise ValueError("label_column must be a non-empty string")

        self.paths = _coerce_paths(paths)
        self.batch_size = batch_size
        self.payload_column = payload_column.strip()
        self.label_column = label_column.strip()
        self.reject_report_path = (
            Path(reject_report_path) if reject_report_path is not None else None
        )
        self.stats = ImportStats()

    def iter_batches(self) -> Iterator[list[DatasetRecord]]:
        """Yield batches of valid records and update ``stats`` as consumed."""
        self.stats = ImportStats()
        batch: list[DatasetRecord] = []

        with tempfile.TemporaryDirectory(prefix="aihoneypot-dataset-import-") as tmp:
            duplicate_db = sqlite3.connect(Path(tmp) / "seen-payloads.sqlite3")
            try:
                duplicate_db.execute(
                    "CREATE TABLE seen_payloads ("
                    "digest BLOB NOT NULL, payload TEXT NOT NULL, "
                    "PRIMARY KEY (digest, payload))"
                )
                with self._open_reject_report() as reject_report:
                    for requested_path in self.paths:
                        try:
                            source_path = requested_path.resolve(strict=True)
                        except OSError as exc:
                            raise DatasetImportError(
                                f"Dataset file is unavailable: {requested_path}"
                            ) from exc

                        for candidate in self._iter_candidates(source_path):
                            self.stats.records_read += 1
                            record, reason = self._validate_candidate(candidate)
                            if reason is not None:
                                self._reject(candidate, reason, reject_report)
                                continue

                            assert record is not None
                            if self._is_duplicate(record.payload, duplicate_db):
                                self.stats.duplicate_payloads += 1

                            self.stats.valid_records += 1
                            self.stats.label_counts[record.label] += 1
                            batch.append(record)
                            if len(batch) == self.batch_size:
                                yield batch
                                batch = []

                    if batch:
                        yield batch
            finally:
                duplicate_db.close()

    def _open_reject_report(self):
        if self.reject_report_path is None:
            return _NullTextWriter()
        try:
            return self.reject_report_path.open(
                "x", encoding="utf-8", newline="\n"
            )
        except OSError as exc:
            raise DatasetImportError(
                f"Reject report could not be created: {self.reject_report_path}"
            ) from exc

    def _iter_candidates(self, path: Path) -> Iterator[_Candidate]:
        suffix = path.suffix.casefold()
        if suffix == ".csv":
            yield from self._iter_csv(path)
        elif suffix in {".jsonl", ".ndjson"}:
            yield from self._iter_jsonl(path)
        elif suffix == ".txt":
            yield from self._iter_txt(path)
        elif suffix == ".json":
            raise UnsupportedDatasetFormatError(
                "JSON files/arrays are unsupported: a streaming JSON parser "
                "is not included; use JSONL or provide an installed streaming parser"
            )
        else:
            raise UnsupportedDatasetFormatError(
                f"Unsupported dataset extension '{suffix or '<none>'}'; "
                "use .csv, .jsonl, .ndjson, or .txt"
            )

    def _iter_csv(self, path: Path) -> Iterator[_Candidate]:
        source = str(path)
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as stream:
                reader = csv.reader(stream)
                try:
                    header = next(reader)
                except StopIteration:
                    return
                except (csv.Error, UnicodeDecodeError) as exc:
                    yield _Candidate(source, 0, error="invalid CSV header")
                    return

                header = [field.strip() for field in header]
                duplicate_headers = len(header) != len(set(header))
                required = {self.payload_column, self.label_column}
                missing_headers = required.difference(header)
                row_number = 0

                while True:
                    try:
                        row = next(reader)
                    except StopIteration:
                        break
                    except (csv.Error, UnicodeDecodeError):
                        row_number += 1
                        yield _Candidate(
                            source,
                            row_number,
                            error="malformed or non-UTF-8 CSV record",
                        )
                        break

                    if not row:
                        continue
                    row_number += 1

                    if duplicate_headers:
                        yield _Candidate(
                            source,
                            row_number,
                            error="CSV header contains duplicate column names",
                        )
                    elif missing_headers:
                        yield _Candidate(
                            source,
                            row_number,
                            error="CSV header is missing required columns",
                        )
                    elif len(row) != len(header):
                        yield _Candidate(
                            source,
                            row_number,
                            error="CSV record has a different number of fields than its header",
                        )
                    else:
                        values = dict(zip(header, row))
                        yield _Candidate(
                            source,
                            row_number,
                            values.get(self.payload_column),
                            values.get(self.label_column),
                        )
        except OSError as exc:
            raise DatasetImportError(f"Could not read CSV dataset: {path}") from exc

    def _iter_jsonl(self, path: Path) -> Iterator[_Candidate]:
        source = str(path)
        try:
            with path.open("rb") as stream:
                for line_number, raw_line in enumerate(stream, start=1):
                    if not raw_line.strip():
                        continue
                    try:
                        line = raw_line.decode("utf-8")
                    except UnicodeDecodeError:
                        yield _Candidate(
                            source, line_number, error="record is not valid UTF-8"
                        )
                        continue
                    try:
                        value = json.loads(
                            line, parse_constant=_reject_json_constant
                        )
                    except (json.JSONDecodeError, ValueError, RecursionError):
                        yield _Candidate(
                            source, line_number, error="record is not valid JSON"
                        )
                        continue
                    if not isinstance(value, dict):
                        yield _Candidate(
                            source, line_number, error="JSONL record must be an object"
                        )
                        continue
                    yield _Candidate(
                        source,
                        line_number,
                        value.get("payload"),
                        value.get("label"),
                    )
        except OSError as exc:
            raise DatasetImportError(f"Could not read JSONL dataset: {path}") from exc

    def _iter_txt(self, path: Path) -> Iterator[_Candidate]:
        source = str(path)
        try:
            with path.open("rb") as stream:
                for line_number, raw_line in enumerate(stream, start=1):
                    if not raw_line.strip():
                        continue
                    try:
                        line = raw_line.decode("utf-8")
                    except UnicodeDecodeError:
                        yield _Candidate(
                            source, line_number, error="record is not valid UTF-8"
                        )
                        continue
                    label, separator, payload = line.rstrip("\r\n").partition("\t")
                    if not separator:
                        yield _Candidate(
                            source,
                            line_number,
                            error="TXT record must use LABEL<TAB>PAYLOAD format",
                        )
                        continue
                    yield _Candidate(source, line_number, payload, label)
        except OSError as exc:
            raise DatasetImportError(f"Could not read TXT dataset: {path}") from exc

    def _validate_candidate(
        self, candidate: _Candidate
    ) -> tuple[Optional[DatasetRecord], Optional[str]]:
        if candidate.error is not None:
            return None, candidate.error
        if not isinstance(candidate.payload, str):
            return None, "payload is missing or is not a string"
        if not candidate.payload.strip():
            self.stats.empty_payloads += 1
            return None, "payload is empty"
        label = normalize_label(candidate.label)
        if label is None:
            if candidate.label is None or (
                isinstance(candidate.label, str) and not candidate.label.strip()
            ):
                return None, "label is missing"
            return None, "label is not one of SQLi, XSS, Normal"
        return (
            DatasetRecord(
                payload=candidate.payload,
                label=label,
                source_file=candidate.source_file,
                source_record=candidate.source_record,
            ),
            None,
        )

    @staticmethod
    def _is_duplicate(payload: str, database: sqlite3.Connection) -> bool:
        digest = hashlib.sha256(payload.encode("utf-8")).digest()
        existing = database.execute(
            "SELECT payload FROM seen_payloads WHERE digest = ?", (digest,)
        )
        for (stored_payload,) in existing:
            if stored_payload == payload:
                return True
        database.execute(
            "INSERT INTO seen_payloads (digest, payload) VALUES (?, ?)",
            (digest, payload),
        )
        return False

    def _reject(
        self,
        candidate: _Candidate,
        reason: str,
        reject_report: TextIO,
    ) -> None:
        self.stats.rejected_records += 1
        json.dump(
            {
                "source_file": candidate.source_file,
                "source_record": candidate.source_record,
                "reason": reason,
            },
            reject_report,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        reject_report.write("\n")


class _NullTextWriter:
    def __enter__(self) -> "_NullTextWriter":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        return False

    def write(self, value: str) -> int:
        return len(value)


def import_dataset(
    paths: Union[PathLike, Iterable[PathLike]],
    *,
    batch_size: int = 1000,
    payload_column: str = "payload",
    label_column: str = "label",
    reject_report_path: Optional[PathLike] = None,
) -> tuple[Iterator[list[DatasetRecord]], DatasetImporter]:
    """Create an importer and return its lazy batch iterator and stats owner."""
    importer = DatasetImporter(
        paths,
        batch_size=batch_size,
        payload_column=payload_column,
        label_column=label_column,
        reject_report_path=reject_report_path,
    )
    return importer.iter_batches(), importer
