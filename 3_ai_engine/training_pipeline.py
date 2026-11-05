"""Offline, disk-backed dataset training and experimental model releases."""

import hashlib
import ctypes
import errno
import json
import logging
import os
import platform
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import unicodedata
import uuid
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import RandomForestClassifier
from sklearn.feature_extraction.text import HashingVectorizer, TfidfVectorizer
from sklearn.linear_model import SGDClassifier

from data_import import LABEL_ALIASES, SUPPORTED_LABELS as IMPORT_LABELS
from data_import import DatasetImporter
from dataset_normalization import DatasetNormalizer, NormalizedDatasetRecord


logger = logging.getLogger(__name__)
LABELS = ("Normal", "SQLi", "XSS")
_MODEL_RANDOM_STATE = 42
_SPLITS = ("train", "validation", "test")
_SPLIT_RATIOS = {"train": 0.70, "validation": 0.15, "test": 0.15}
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class TrainingPipelineError(RuntimeError):
    """Raised when a valid, reproducible training run cannot be completed."""


class _ModelContractError(TrainingPipelineError):
    """Raised when a trained or deserialized model violates its API contract."""


@dataclass(frozen=True)
class TrainingConfig:
    """Resource and reproducibility settings for an offline training run."""

    random_seed: int = 42
    batch_size: int = 512
    n_features: int = 262144
    incremental_epochs: int = 1
    baseline_max_samples: int = 5000
    baseline_max_total_payload_chars: int = 5_000_000
    baseline_max_payload_chars: int = 100_000
    small_class_warning_threshold: int = 20

    def __post_init__(self) -> None:
        for name in (
            "random_seed",
            "batch_size",
            "n_features",
            "incremental_epochs",
            "baseline_max_samples",
            "baseline_max_total_payload_chars",
            "baseline_max_payload_chars",
            "small_class_warning_threshold",
        ):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} must be an integer")
        if self.batch_size < 1:
            raise ValueError("batch_size must be positive")
        if self.n_features < 1:
            raise ValueError("n_features must be positive")
        if self.incremental_epochs < 1:
            raise ValueError("incremental_epochs must be positive")
        if self.baseline_max_samples < 1:
            raise ValueError("baseline_max_samples must be positive")
        if self.baseline_max_total_payload_chars < 1:
            raise ValueError("baseline_max_total_payload_chars must be positive")
        if self.baseline_max_payload_chars < 1:
            raise ValueError("baseline_max_payload_chars must be positive")
        if self.small_class_warning_threshold < 1:
            raise ValueError("small_class_warning_threshold must be positive")


@dataclass(frozen=True)
class TrainingRunResult:
    """Summary of a completed run and its immutable experimental release."""

    release_path: Path
    metadata: dict[str, Any]


def _allocate_split_counts(sample_count: int) -> dict[str, int]:
    """Allocate all rows using largest remainder, then ensure each split exists."""
    if sample_count < len(_SPLITS):
        raise TrainingPipelineError(
            "Each class needs at least 3 eligible unique records to appear in "
            "train, validation, and test; received "
            f"{sample_count}"
        )

    exact = {
        split: sample_count * _SPLIT_RATIOS[split]
        for split in _SPLITS
    }
    counts = {split: int(exact[split]) for split in _SPLITS}
    remaining = sample_count - sum(counts.values())
    remainder_order = sorted(
        _SPLITS,
        key=lambda split: (-(exact[split] - counts[split]), _SPLITS.index(split)),
    )
    for split in remainder_order[:remaining]:
        counts[split] += 1

    for missing_split in _SPLITS:
        if counts[missing_split] == 0:
            donors = [split for split in _SPLITS if counts[split] > 1]
            if not donors:
                raise TrainingPipelineError(
                    "Could not represent every class in all three dataset splits"
                )
            donor = max(
                donors,
                key=lambda split: (
                    counts[split] - exact[split],
                    counts[split],
                    -_SPLITS.index(split),
                ),
            )
            counts[donor] -= 1
            counts[missing_split] += 1
    return counts


def _create_staging_schema(database: sqlite3.Connection) -> None:
    database.execute(
        """
        CREATE TABLE records (
            fingerprint TEXT PRIMARY KEY,
            normalized_payload TEXT NOT NULL,
            payload TEXT NOT NULL,
            label TEXT NOT NULL CHECK (label IN ('Normal', 'SQLi', 'XSS')),
            source_file TEXT NOT NULL,
            source_record INTEGER NOT NULL,
            split TEXT,
            split_key TEXT
        )
        """
    )


def _stage_normalized_batches(
    database: sqlite3.Connection,
    normalized_batches: Iterable[Iterable[NormalizedDatasetRecord]],
) -> int:
    """Persist normalized records without retaining prior batches in memory."""
    inserted = 0
    for batch in normalized_batches:
        for record in batch:
            if not isinstance(record.payload, str) or not record.payload.strip():
                raise TrainingPipelineError(
                    "Normalizer emitted a missing or empty payload"
                )
            if record.label not in LABELS:
                raise TrainingPipelineError(
                    f"Normalizer emitted unsupported label {record.label!r}"
                )
            if (
                not isinstance(record.source_file, str)
                or not record.source_file
                or type(record.source_record) is not int
                or record.source_record < 1
            ):
                raise TrainingPipelineError(
                    "Normalizer emitted invalid source traceability fields"
                )

            normalized_payload = unicodedata.normalize("NFC", record.payload)
            fingerprint = hashlib.sha256(
                normalized_payload.encode("utf-8")
            ).hexdigest()
            existing = database.execute(
                "SELECT normalized_payload, label FROM records WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if existing is not None:
                if existing[0] != normalized_payload:
                    raise TrainingPipelineError(
                        "SHA-256 collision detected in normalized payloads"
                    )
                if existing[1] != record.label:
                    raise TrainingPipelineError(
                        "Normalizer emitted a payload with conflicting labels"
                    )
                raise TrainingPipelineError(
                    "Normalizer emitted a duplicate normalized payload"
                )
            database.execute(
                """
                INSERT INTO records (
                    fingerprint, normalized_payload, payload, label,
                    source_file, source_record
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    fingerprint,
                    normalized_payload,
                    record.payload,
                    record.label,
                    record.source_file,
                    record.source_record,
                ),
            )
            inserted += 1
        database.commit()
    database.execute(
        "CREATE INDEX records_label_fingerprint ON records(label, fingerprint)"
    )
    return inserted


def _close_iterators_preserving_error(
    iterators: Sequence[object], primary_error: Optional[BaseException]
) -> None:
    cleanup_error: Optional[Exception] = None
    for iterator in iterators:
        close = getattr(iterator, "close", None)
        if not callable(close):
            continue
        try:
            close()
        except Exception as exc:
            if primary_error is not None:
                add_note = getattr(primary_error, "add_note", None)
                if callable(add_note):
                    add_note(f"Iterator cleanup also failed: {exc}")
                else:
                    try:
                        warnings.warn(
                            f"Iterator cleanup failed while preserving the "
                            f"original error: {exc}",
                            RuntimeWarning,
                            stacklevel=2,
                        )
                    except Warning:
                        pass
            elif cleanup_error is None:
                cleanup_error = exc
    if cleanup_error is not None:
        raise cleanup_error


def _stage_and_close_iterators(
    database: sqlite3.Connection,
    importer_batches: Iterable[Iterable[object]],
    normalized_batches: Iterable[Iterable[NormalizedDatasetRecord]],
) -> int:
    primary_error: Optional[BaseException] = None
    try:
        return _stage_normalized_batches(database, normalized_batches)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        _close_iterators_preserving_error(
            (normalized_batches, importer_batches),
            primary_error,
        )


def _class_counts(database: sqlite3.Connection) -> dict[str, int]:
    counts = {label: 0 for label in LABELS}
    for label, count in database.execute(
        "SELECT label, COUNT(*) FROM records GROUP BY label"
    ):
        counts[label] = count
    return counts


def _assign_splits(
    database: sqlite3.Connection,
    random_seed: int,
    small_class_warning_threshold: int = 20,
) -> dict[str, dict[str, int]]:
    """Assign every unique fingerprint deterministically within each class."""
    if type(small_class_warning_threshold) is not int:
        raise TypeError("small_class_warning_threshold must be an integer")
    if small_class_warning_threshold < 1:
        raise ValueError("small_class_warning_threshold must be positive")
    counts_by_label = _class_counts(database)
    missing_labels = [label for label in LABELS if counts_by_label[label] == 0]
    if missing_labels:
        raise TrainingPipelineError(
            "Training data must contain every canonical class; missing "
            + ", ".join(missing_labels)
        )

    result = {
        split: {label: 0 for label in LABELS}
        for split in _SPLITS
    }
    seed_text = str(random_seed)
    for label in LABELS:
        class_count = counts_by_label[label]
        if class_count < len(_SPLITS):
            raise TrainingPipelineError(
                f"Class {label} has {class_count} eligible unique record(s); "
                "at least 3 are required to populate every split"
            )
        if class_count < small_class_warning_threshold:
            warnings.warn(
                f"Class {label} has only {class_count} eligible unique samples; "
                "evaluation may have weak statistical support.",
                RuntimeWarning,
                stacklevel=2,
            )

        split_counts = _allocate_split_counts(class_count)
        rows = database.execute(
            "SELECT fingerprint FROM records WHERE label = ?", (label,)
        )
        for (fingerprint,) in rows:
            split_key = hashlib.sha256(
                f"{seed_text}\0{label}\0{fingerprint}".encode("utf-8")
            ).hexdigest()
            database.execute(
                "UPDATE records SET split_key = ? WHERE fingerprint = ?",
                (split_key, fingerprint),
            )
        database.commit()

        position = 0
        cursor = database.execute(
            "SELECT fingerprint FROM records WHERE label = ? "
            "ORDER BY split_key, fingerprint",
            (label,),
        )
        for (fingerprint,) in cursor:
            for split in _SPLITS:
                next_boundary = sum(
                    split_counts[name]
                    for name in _SPLITS[: _SPLITS.index(split) + 1]
                )
                if position < next_boundary:
                    selected_split = split
                    break
            database.execute(
                "UPDATE records SET split = ? WHERE fingerprint = ?",
                (selected_split, fingerprint),
            )
            result[selected_split][label] += 1
            position += 1
        database.commit()
        if position != class_count:
            raise TrainingPipelineError(
                f"Split assignment lost records for class {label}"
            )
        if any(result[split][label] == 0 for split in _SPLITS):
            raise TrainingPipelineError(
                f"Could not represent class {label} in every split"
            )

    total_rows = database.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    assigned_rows = database.execute(
        "SELECT COUNT(*) FROM records WHERE split IS NOT NULL"
    ).fetchone()[0]
    if total_rows != assigned_rows:
        raise TrainingPipelineError(
            "Not all eligible unique records received a split assignment"
        )
    return result


def _split_assignment_digest(database: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for fingerprint, split in database.execute(
        "SELECT fingerprint, split FROM records ORDER BY fingerprint"
    ):
        digest.update(f"{fingerprint}\0{split}\n".encode("ascii"))
    return digest.hexdigest()


def _iter_split_batches(
    database: sqlite3.Connection,
    split: str,
    batch_size: int,
) -> Iterator[tuple[list[str], list[str]]]:
    if split not in _SPLITS:
        raise ValueError(f"Unknown split: {split}")
    cursor = database.execute(
        "SELECT payload, label FROM records "
        "WHERE split = ? ORDER BY split_key, fingerprint",
        (split,),
    )
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            return
        yield [row[0] for row in rows], [row[1] for row in rows]


def _train_incremental_model(
    database: sqlite3.Connection, config: TrainingConfig
) -> tuple[HashingVectorizer, SGDClassifier, float]:
    vectorizer = HashingVectorizer(
        analyzer="char",
        ngram_range=(1, 3),
        alternate_sign=False,
        norm="l2",
        n_features=config.n_features,
    )
    model = SGDClassifier(
        loss="log_loss",
        random_state=_MODEL_RANDOM_STATE,
    )
    started = time.perf_counter()
    initialized = False
    for _ in range(config.incremental_epochs):
        for payloads, labels in _iter_split_batches(
            database, "train", config.batch_size
        ):
            features = vectorizer.transform(payloads)
            if not initialized:
                model.partial_fit(
                    features,
                    labels,
                    classes=np.asarray(LABELS, dtype=object),
                )
                initialized = True
            else:
                model.partial_fit(features, labels)
    duration = time.perf_counter() - started
    if not initialized:
        raise TrainingPipelineError("Training split is empty")
    _verify_prediction_contract(vectorizer, model, database)
    return vectorizer, model, duration


def _verify_prediction_contract(
    vectorizer: Any, model: Any, database: sqlite3.Connection
) -> None:
    first = database.execute(
        "SELECT payload FROM records WHERE split = 'train' LIMIT 1"
    ).fetchone()
    if first is None:
        raise _ModelContractError("Cannot verify a model without training records")
    if not callable(getattr(vectorizer, "transform", None)):
        raise _ModelContractError(
            "Trained feature transformation does not expose transform()"
        )
    if not callable(getattr(model, "predict", None)):
        raise _ModelContractError("Trained classifier does not expose predict()")
    if not callable(getattr(model, "predict_proba", None)):
        raise _ModelContractError(
            "Trained classifier does not expose predict_proba()"
        )
    if not hasattr(model, "classes_"):
        raise _ModelContractError("Trained classifier does not expose classes_")
    features = vectorizer.transform([first[0]])
    prediction = model.predict(features)
    probabilities = model.predict_proba(features)
    model_classes = tuple(str(label) for label in model.classes_)
    if set(model_classes) != set(LABELS) or len(model_classes) != len(LABELS):
        raise _ModelContractError(
            "Trained classifier classes_ does not contain all canonical labels"
        )
    if len(prediction) != 1 or probabilities.shape != (1, len(LABELS)):
        raise _ModelContractError(
            "Trained model does not satisfy the predict/predict_proba contract"
        )
    if not np.isfinite(probabilities).all():
        raise _ModelContractError("Model returned non-finite probabilities")
    if prediction[0] not in LABELS:
        raise _ModelContractError("Model predicted a non-canonical label")


def _baseline_limit_failure(
    code: str,
    message: str,
    observed: int,
    limit: int,
) -> dict[str, Any]:
    return {
        "code": code,
        "stage": "resource_preflight",
        "message": message,
        "observed": observed,
        "limit": limit,
    }


def _check_baseline_resource_limits(
    database: sqlite3.Connection, config: TrainingConfig
) -> Optional[dict[str, Any]]:
    train_count = database.execute(
        "SELECT COUNT(*) FROM records WHERE split = 'train'"
    ).fetchone()[0]
    if train_count > config.baseline_max_samples:
        return _baseline_limit_failure(
            "sample_count_limit_exceeded",
            "Baseline training sample count exceeds its configured limit",
            train_count,
            config.baseline_max_samples,
        )

    total_characters = 0
    cursor = database.execute(
        "SELECT payload FROM records WHERE split = 'train' "
        "ORDER BY split_key, fingerprint"
    )
    while True:
        rows = cursor.fetchmany(config.batch_size)
        if not rows:
            break
        for (payload,) in rows:
            payload_length = len(payload)
            if payload_length > config.baseline_max_payload_chars:
                return _baseline_limit_failure(
                    "individual_payload_limit_exceeded",
                    "A baseline training payload exceeds its configured "
                    "character limit",
                    payload_length,
                    config.baseline_max_payload_chars,
                )
            total_characters += payload_length
            if total_characters > config.baseline_max_total_payload_chars:
                return _baseline_limit_failure(
                    "total_payload_characters_limit_exceeded",
                    "Baseline training payloads exceed their configured total "
                    "character limit",
                    total_characters,
                    config.baseline_max_total_payload_chars,
                )
    return None


def _train_baseline(
    database: sqlite3.Connection, config: TrainingConfig
) -> tuple[
    Optional[TfidfVectorizer],
    Optional[RandomForestClassifier],
    float,
    Optional[dict[str, Any]],
]:
    started = time.perf_counter()
    stage = "resource_preflight"
    try:
        resource_failure = _check_baseline_resource_limits(database, config)
        if resource_failure is not None:
            return None, None, time.perf_counter() - started, resource_failure

        stage = "data_extraction"
        payloads: list[str] = []
        labels: list[str] = []
        for payload_batch, label_batch in _iter_split_batches(
            database, "train", config.batch_size
        ):
            payloads.extend(payload_batch)
            labels.extend(label_batch)

        stage = "tfidf_fit_transform"
        vectorizer = TfidfVectorizer(analyzer="char", ngram_range=(1, 3))
        model = RandomForestClassifier(
            n_estimators=100,
            random_state=_MODEL_RANDOM_STATE,
        )
        features = vectorizer.fit_transform(payloads)

        stage = "random_forest_fit"
        model.fit(features, labels)

        stage = "prediction_contract"
        _verify_prediction_contract(vectorizer, model, database)
    except MemoryError as exc:
        return (
            None,
            None,
            time.perf_counter() - started,
            {
                "code": "memory_error",
                "stage": stage,
                "message": str(exc) or "Baseline ran out of available memory",
                "exception_type": type(exc).__name__,
            },
        )
    except _ModelContractError as exc:
        return (
            None,
            None,
            time.perf_counter() - started,
            {
                "code": "model_contract_failure",
                "stage": stage,
                "message": str(exc),
                "exception_type": type(exc).__name__,
            },
        )
    except (sqlite3.Error, OSError) as exc:
        return (
            None,
            None,
            time.perf_counter() - started,
            {
                "code": "operational_error",
                "stage": stage,
                "message": str(exc) or "Baseline encountered an I/O failure",
                "exception_type": type(exc).__name__,
            },
        )
    return vectorizer, model, time.perf_counter() - started, None


def _metrics_from_confusion(matrix: np.ndarray) -> dict[str, Any]:
    matrix = np.asarray(matrix, dtype=np.int64)
    supports = matrix.sum(axis=1)
    predicted_counts = matrix.sum(axis=0)
    per_class: dict[str, dict[str, Any]] = {}
    f1_values: list[float] = []
    total = int(matrix.sum())

    for index, label in enumerate(LABELS):
        true_positive = int(matrix[index, index])
        support = int(supports[index])
        predicted = int(predicted_counts[index])
        precision = true_positive / predicted if predicted else 0.0
        recall = true_positive / support if support else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        )
        f1_values.append(f1)
        per_class[label] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": support,
        }

    return {
        "accuracy": float(np.trace(matrix) / total) if total else 0.0,
        "per_class": per_class,
        "macro_f1": float(sum(f1_values) / len(LABELS)),
        "confusion_matrix": matrix.tolist(),
        "class_order": list(LABELS),
        "total_samples": total,
        "zero_division": 0,
    }


def _evaluate_model(
    database: sqlite3.Connection,
    split: str,
    vectorizer: Any,
    model: Any,
    batch_size: int,
) -> dict[str, Any]:
    if split not in {"validation", "test"}:
        raise ValueError("Only validation and test splits may be evaluated")
    matrix = np.zeros((len(LABELS), len(LABELS)), dtype=np.int64)
    label_indexes = {label: index for index, label in enumerate(LABELS)}
    started = time.perf_counter()
    for payloads, actual_labels in _iter_split_batches(
        database, split, batch_size
    ):
        predicted_labels = model.predict(vectorizer.transform(payloads))
        if len(predicted_labels) != len(actual_labels):
            raise _ModelContractError(
                "Model prediction count does not match evaluation batch size"
            )
        for actual, predicted in zip(actual_labels, predicted_labels):
            if actual not in label_indexes or predicted not in label_indexes:
                raise _ModelContractError(
                    "Model produced a label outside the canonical taxonomy"
                )
            matrix[label_indexes[actual], label_indexes[predicted]] += 1
    result = _metrics_from_confusion(matrix)
    result["evaluation_duration_seconds"] = time.perf_counter() - started
    return result


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _dataset_provenance(paths: Sequence[Path]) -> list[dict[str, Any]]:
    provenance = []
    for path in paths:
        resolved = path.resolve(strict=True)
        stat = resolved.stat()
        provenance.append(
            {
                "source_file": str(resolved),
                "size_bytes": stat.st_size,
                "sha256": _file_sha256(resolved),
            }
        )
    return provenance


def _comparison(candidates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    baseline = candidates["baseline"]
    alternative = candidates["alternative"]
    if baseline["status"] != "completed" or alternative["status"] != "completed":
        return {
            "status": "incomplete",
            "reason": "Both candidates must complete evaluation on the same splits",
        }

    baseline_validation = baseline["metrics"]["validation"]
    alternative_validation = alternative["metrics"]["validation"]
    baseline_f1 = baseline_validation["macro_f1"]
    alternative_f1 = alternative_validation["macro_f1"]
    if baseline_f1 > alternative_f1:
        higher_macro_f1 = "baseline"
    elif alternative_f1 > baseline_f1:
        higher_macro_f1 = "alternative"
    else:
        higher_macro_f1 = "tie"

    per_class = {}
    for label in LABELS:
        baseline_f1_label = baseline_validation["per_class"][label]["f1"]
        alternative_f1_label = alternative_validation["per_class"][label]["f1"]
        if baseline_f1_label > alternative_f1_label:
            stronger = "baseline"
        elif alternative_f1_label > baseline_f1_label:
            stronger = "alternative"
        else:
            stronger = "tie"
        per_class[label] = {
            "baseline_f1": baseline_f1_label,
            "alternative_f1": alternative_f1_label,
            "higher_f1": stronger,
        }
    return {
        "status": "complete",
        "evaluated_split": "validation",
        "final_test_metrics_reported_independently": True,
        "higher_macro_f1": higher_macro_f1,
        "per_class": per_class,
        "training_duration_seconds": {
            "baseline": baseline["training_duration_seconds"],
            "alternative": alternative["training_duration_seconds"],
        },
        "limitations": [
            "The baseline is limited by the configured training-sample safety cap.",
            "Training duration alone does not establish model quality.",
        ],
    }


def _release_version(version: Optional[str], random_seed: int) -> str:
    if version is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{timestamp}-seed{random_seed}-{uuid.uuid4().hex[:8]}"
    if not isinstance(version, str) or not _VERSION_PATTERN.fullmatch(version):
        raise ValueError(
            "version must contain 1-128 letters, digits, dots, underscores, "
            "or hyphens and start with a letter or digit"
        )
    return version


def _write_json(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def _verify_serialized_candidate(
    model_path: Path,
    transform_path: Path,
    expected_classes: Sequence[str],
) -> None:
    model = joblib.load(model_path)
    transform = joblib.load(transform_path)
    if not callable(getattr(model, "predict", None)):
        raise _ModelContractError("Serialized model does not expose predict()")
    if not callable(getattr(model, "predict_proba", None)):
        raise _ModelContractError("Serialized model does not expose predict_proba()")
    if not hasattr(model, "classes_"):
        raise _ModelContractError("Serialized model does not expose classes_")
    if not callable(getattr(transform, "transform", None)):
        raise _ModelContractError(
            "Serialized feature transformation does not expose transform()"
        )

    classes = tuple(str(label) for label in model.classes_)
    if (
        len(classes) != len(LABELS)
        or set(classes) != set(LABELS)
        or tuple(expected_classes) != classes
    ):
        raise _ModelContractError(
            "Serialized model classes_ do not match the recorded canonical classes"
        )
    transformed = transform.transform(["synthetic bundle contract payload"])
    if not hasattr(transformed, "shape") or transformed.shape[0] != 1:
        raise _ModelContractError(
            "Serialized feature transformation returned an invalid shape"
        )
    predictions = model.predict(transformed)
    probabilities = model.predict_proba(transformed)
    if len(predictions) != 1 or predictions[0] not in LABELS:
        raise _ModelContractError(
            "Serialized model returned an invalid synthetic prediction"
        )
    if probabilities.shape != (1, len(classes)):
        raise _ModelContractError(
            "Serialized model returned an invalid synthetic probability shape"
        )
    if (
        not np.isfinite(probabilities).all()
        or not np.isclose(probabilities.sum(), 1.0, rtol=1e-5, atol=1e-5)
    ):
        raise _ModelContractError(
            "Serialized model returned invalid synthetic probabilities"
        )


def _rename_directory_noreplace_posix(
    source: Path, destination: Path
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise TrainingPipelineError(
            "This POSIX platform does not expose renameat2; refusing to publish "
            "without an atomic no-clobber operation"
        )
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(destination),
        1,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(
            error_number,
            os.strerror(error_number),
            str(destination),
        )
    if error_number in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
        raise TrainingPipelineError(
            "The filesystem does not support atomic RENAME_NOREPLACE; "
            "refusing to publish the release"
        )
    raise OSError(error_number, os.strerror(error_number), str(destination))


def _rename_directory_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename without replacement on supported local filesystems."""
    if platform.system() == "Windows":
        # Windows MoveFile semantics used by os.rename fail if destination exists.
        os.rename(source, destination)
    elif platform.system() == "Linux":
        _rename_directory_noreplace_posix(source, destination)
    else:
        raise TrainingPipelineError(
            "No-clobber atomic directory publication is supported only on "
            f"Windows and Linux, not {platform.system()!r}"
        )


def _log_rename_failure(
    source: Path,
    destination: Path,
    rename_error: OSError,
) -> None:
    diagnostics: dict[str, Any] = {}
    diagnostic_errors: list[str] = []

    def capture(name: str, operation) -> Any:
        try:
            value = operation()
        except FileNotFoundError:
            return None
        except Exception as exc:
            diagnostic_errors.append(f"{name}: {type(exc).__name__}: {exc}")
            return None
        return value

    source_absolute = capture(
        "source_absolute", lambda: os.path.abspath(os.fspath(source))
    )
    destination_absolute = capture(
        "destination_absolute", lambda: os.path.abspath(os.fspath(destination))
    )
    source_stat = capture("source_stat", source.stat)
    destination_lstat = capture("destination_lstat", destination.lstat)
    parent_stat = capture("destination_parent_stat", destination.parent.stat)
    error_errno = capture("exception_errno", lambda: rename_error.errno)
    error_winerror = capture(
        "exception_winerror", lambda: getattr(rename_error, "winerror", None)
    )
    error_message = capture("exception_message", lambda: str(rename_error))

    diagnostics.update(
        {
            "source_exists": source_stat is not None,
            "source_is_dir": (
                stat.S_ISDIR(source_stat.st_mode) if source_stat is not None else None
            ),
            "destination_exists": destination_lstat is not None,
            "destination_is_symlink": (
                stat.S_ISLNK(destination_lstat.st_mode)
                if destination_lstat is not None
                else None
            ),
            "source_st_dev": source_stat.st_dev if source_stat is not None else None,
            "destination_parent_st_dev": (
                parent_stat.st_dev if parent_stat is not None else None
            ),
        }
    )
    if source_absolute is None:
        source_absolute = str(source)
    if destination_absolute is None:
        destination_absolute = str(destination)

    logger.error(
        "Model release rename failed; staging is incomplete/unpublished: "
        "source=%s destination=%s exception_class=%s errno=%r winerror=%r "
        "message=%s diagnostics=%r diagnostic_errors=%r",
        source_absolute,
        destination_absolute,
        type(rename_error).__name__,
        error_errno,
        error_winerror,
        error_message,
        diagnostics,
        diagnostic_errors,
        exc_info=(
            type(rename_error),
            rename_error,
            rename_error.__traceback__,
        ),
    )


def _publish_release(
    release_root: Path,
    version: str,
    candidates: dict[str, dict[str, Any]],
    metadata: dict[str, Any],
) -> Path:
    release_root.mkdir(parents=True, exist_ok=True)
    release_root = release_root.resolve(strict=True)
    destination = release_root / version
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Experimental release already exists: {destination}")

    staging = Path(
        tempfile.mkdtemp(prefix=f".{version}.staging-", dir=str(release_root))
    )
    rename_failed = False
    try:
        for candidate_name, candidate in candidates.items():
            if candidate["status"] != "completed":
                continue
            candidate_directory = staging / candidate_name
            candidate_directory.mkdir()
            joblib.dump(candidate["model"], candidate_directory / "model.joblib")
            joblib.dump(
                candidate["vectorizer"],
                candidate_directory / "feature_transform.joblib",
            )
            _verify_serialized_candidate(
                candidate_directory / "model.joblib",
                candidate_directory / "feature_transform.joblib",
                candidate["classes"],
            )

        _write_json(
            staging / "label_mapping.json",
            {
                "canonical_labels": list(LABELS),
                "aliases": LABEL_ALIASES,
            },
        )
        _write_json(
            staging / "preprocessing.json",
            {
                "payload_preserved": True,
                "duplicate_fingerprint_normalization": "Unicode NFC",
                "payload_transform_for_model": "candidate-specific vectorizer",
                "threshold_selection": "not performed",
            },
        )
        _write_json(staging / "metadata.json", metadata)
        required = [
            staging / "label_mapping.json",
            staging / "preprocessing.json",
            staging / "metadata.json",
        ]
        if any(not path.is_file() for path in required):
            raise TrainingPipelineError(
                "Release staging directory is missing required metadata"
            )
        for candidate_name, candidate in candidates.items():
            if candidate["status"] == "completed":
                candidate_directory = staging / candidate_name
                if not (candidate_directory / "model.joblib").is_file() or not (
                    candidate_directory / "feature_transform.joblib"
                ).is_file():
                    raise TrainingPipelineError(
                        f"Release is missing {candidate_name} model files"
                    )
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(
                f"Experimental release already exists: {destination}"
            )
        if staging.stat().st_dev != release_root.stat().st_dev:
            raise TrainingPipelineError(
                "Release staging and destination are not on the same filesystem"
            )
        try:
            _rename_directory_noreplace(staging, destination)
        except OSError as rename_error:
            rename_failed = True
            _log_rename_failure(staging, destination, rename_error)
            raise
    except BaseException as publication_error:
        if not rename_failed and staging.exists():
            try:
                shutil.rmtree(staging)
            except OSError as cleanup_error:
                add_note = getattr(publication_error, "add_note", None)
                if callable(add_note):
                    add_note(
                        f"Could not remove this run's staging directory "
                        f"{staging}: {cleanup_error}"
                    )
                else:
                    try:
                        warnings.warn(
                            f"Could not remove failed release staging directory "
                            f"{staging}: {cleanup_error}",
                            RuntimeWarning,
                            stacklevel=2,
                        )
                    except Warning:
                        pass
        raise
    return destination


def run_training_pipeline(
    dataset_paths: Any,
    *,
    release_root: Optional[os.PathLike[str] | str] = None,
    temp_root: Optional[os.PathLike[str] | str] = None,
    config: Optional[TrainingConfig] = None,
    version: Optional[str] = None,
) -> TrainingRunResult:
    """Import, normalize, split, train, evaluate, and publish an offline run.

    ``dataset_paths`` is passed to the existing DatasetImporter and may be a
    path or an iterable of paths. Any class with fewer than three eligible
    unique normalized payloads fails before training. Confidence-threshold
    selection is intentionally not performed; the test split is reserved for
    final evaluation only.
    """
    if IMPORT_LABELS and set(IMPORT_LABELS) != set(LABELS):
        raise TrainingPipelineError(
            "Importer taxonomy does not match the training pipeline taxonomy"
        )
    selected_config = config or TrainingConfig()
    selected_version = _release_version(version, selected_config.random_seed)
    destination_root = (
        Path(release_root)
        if release_root is not None
        else Path(__file__).resolve().parent / "artifacts" / "releases"
    )
    destination = destination_root / selected_version
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Experimental release already exists: {destination}")

    importer = DatasetImporter(dataset_paths, batch_size=selected_config.batch_size)
    normalizer = DatasetNormalizer(batch_size=selected_config.batch_size)
    source_paths = tuple(importer.paths)
    provenance = _dataset_provenance(source_paths)
    temporary_directory = tempfile.TemporaryDirectory(
        prefix="aihoneypot-training-",
        dir=str(temp_root) if temp_root is not None else None,
    )
    try:
        database = sqlite3.connect(
            Path(temporary_directory.name) / "training-stage.sqlite3"
        )
        try:
            _create_staging_schema(database)
            imported_batches = importer.iter_batches()
            normalized_batches = normalizer.iter_batches(imported_batches)
            unique_count = _stage_and_close_iterators(
                database,
                imported_batches,
                normalized_batches,
            )
            database.commit()
            split_counts = _assign_splits(
                database,
                selected_config.random_seed,
                selected_config.small_class_warning_threshold,
            )
            split_digest = _split_assignment_digest(database)

            alternative_vectorizer, alternative_model, alternative_duration = (
                _train_incremental_model(database, selected_config)
            )
            candidates: dict[str, dict[str, Any]] = {
                "alternative": {
                    "status": "completed",
                    "model": alternative_model,
                    "vectorizer": alternative_vectorizer,
                    "classes": [str(label) for label in alternative_model.classes_],
                    "training_duration_seconds": alternative_duration,
                    "config": {
                        "vectorizer": "HashingVectorizer",
                        "analyzer": "char",
                        "ngram_range": [1, 3],
                        "alternate_sign": False,
                        "norm": "l2",
                        "n_features": selected_config.n_features,
                        "classifier": "SGDClassifier",
                        "loss": "log_loss",
                        "incremental_epochs": selected_config.incremental_epochs,
                        "random_state": _MODEL_RANDOM_STATE,
                    },
                    "metrics": {},
                },
                "baseline": {
                    "status": "pending",
                    "model": None,
                    "vectorizer": None,
                    "training_duration_seconds": 0.0,
                    "config": {
                        "vectorizer": "TfidfVectorizer",
                        "analyzer": "char",
                        "ngram_range": [1, 3],
                        "classifier": "RandomForestClassifier",
                        "n_estimators": 100,
                        "random_state": _MODEL_RANDOM_STATE,
                        "safety_limit_train_samples": (
                            selected_config.baseline_max_samples
                        ),
                        "safety_limit_total_payload_chars": (
                            selected_config.baseline_max_total_payload_chars
                        ),
                        "safety_limit_individual_payload_chars": (
                            selected_config.baseline_max_payload_chars
                        ),
                    },
                    "metrics": {},
                },
            }
            baseline_result = _train_baseline(database, selected_config)
            (
                baseline_vectorizer,
                baseline_model,
                baseline_duration,
                baseline_skip_reason,
            ) = baseline_result
            if baseline_skip_reason is None:
                candidates["baseline"].update(
                    {
                        "status": "completed",
                        "model": baseline_model,
                        "vectorizer": baseline_vectorizer,
                        "classes": [
                            str(label) for label in baseline_model.classes_
                        ],
                        "training_duration_seconds": baseline_duration,
                    }
                )
            else:
                baseline_status = (
                    "skipped"
                    if baseline_skip_reason["code"].endswith("_limit_exceeded")
                    else "failed"
                )
                candidates["baseline"].update(
                    {
                        "status": baseline_status,
                        "failure": baseline_skip_reason,
                    }
                )
                warnings.warn(
                    "Optional baseline "
                    f"{baseline_status}: {baseline_skip_reason['message']}",
                    RuntimeWarning,
                    stacklevel=2,
                )

            for candidate_name, candidate in candidates.items():
                if candidate["status"] != "completed":
                    continue
                current_evaluation_split = "validation"
                try:
                    candidate["metrics"] = {}
                    for current_evaluation_split in ("validation", "test"):
                        candidate["metrics"][current_evaluation_split] = (
                            _evaluate_model(
                                database,
                                current_evaluation_split,
                                candidate["vectorizer"],
                                candidate["model"],
                                selected_config.batch_size,
                            )
                        )
                except (
                    MemoryError,
                    _ModelContractError,
                    sqlite3.Error,
                    OSError,
                ) as exc:
                    if candidate_name != "baseline":
                        raise
                    if isinstance(exc, MemoryError):
                        failure_code = "memory_error"
                    elif isinstance(exc, _ModelContractError):
                        failure_code = "model_contract_failure"
                    else:
                        failure_code = "operational_error"
                    candidate["status"] = "failed"
                    candidate["failure"] = {
                        "code": failure_code,
                        "stage": f"{current_evaluation_split}_evaluation",
                        "message": str(exc)
                        or "Baseline evaluation failed",
                        "exception_type": type(exc).__name__,
                    }
                    candidate["metrics"] = {}
                    candidate.pop("model", None)
                    candidate.pop("vectorizer", None)
                    candidate.pop("classes", None)
                    warnings.warn(
                        "Optional baseline failed during "
                        f"{current_evaluation_split} evaluation: {exc}",
                        RuntimeWarning,
                        stacklevel=2,
                    )

            comparison = _comparison(candidates)
            for candidate in candidates.values():
                candidate.pop("model", None)
                candidate.pop("vectorizer", None)
            preprocessing = {
                "payload_preserved": True,
                "duplicate_fingerprint_normalization": "Unicode NFC",
                "payload_transform_for_model": "candidate-specific vectorizer",
                "threshold_selection": "not performed",
            }
            for candidate_name, candidate in candidates.items():
                if candidate["status"] != "completed":
                    continue
                candidate["name"] = candidate_name
                candidate["model_filename"] = "model.joblib"
                candidate["transformer_filename"] = "feature_transform.joblib"
                candidate["algorithm"] = {
                    "classifier": candidate["config"]["classifier"],
                    "transformer": candidate["config"]["vectorizer"],
                }
                candidate["canonical_classes"] = list(LABELS)
                candidate["training_configuration"] = dict(candidate["config"])
                candidate["evaluation_metrics"] = dict(candidate["metrics"])
            metadata = {
                "schema_version": 1,
                "release_version": selected_version,
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "preprocessing": preprocessing,
                "random_seed": selected_config.random_seed,
                "training_config": asdict(selected_config),
                "dataset_provenance": provenance,
                "import_stats": asdict(importer.stats),
                "normalization_stats": asdict(normalizer.stats),
                "eligible_unique_records": unique_count,
                "split_counts": split_counts,
                "split_assignment_sha256": split_digest,
                "candidates": candidates,
                "comparison": comparison,
                "threshold_selection": {
                    "performed": False,
                    "reason": (
                        "Serving confidence-threshold tuning is not part of this "
                        "offline pipeline."
                    ),
                    "test_data_used": False,
                },
                "library_versions": {
                    "python": platform.python_version(),
                    "scikit_learn": sklearn.__version__,
                    "joblib": joblib.__version__,
                    "numpy": np.__version__,
                },
            }
            for candidate in candidates.values():
                if candidate["status"] == "completed":
                    candidate["bundle_files"] = {
                        "model": "model.joblib",
                        "feature_transform": "feature_transform.joblib",
                    }
            # Candidate artifacts are retained separately from the JSON report.
            release_candidates = {
                name: {
                    **candidate,
                    "model": (
                        baseline_model
                        if name == "baseline" and candidate["status"] == "completed"
                        else alternative_model
                        if name == "alternative"
                        else None
                    ),
                    "vectorizer": (
                        baseline_vectorizer
                        if name == "baseline" and candidate["status"] == "completed"
                        else alternative_vectorizer
                        if name == "alternative"
                        else None
                    ),
                }
                for name, candidate in candidates.items()
            }
            release_path = _publish_release(
                destination_root,
                selected_version,
                release_candidates,
                metadata,
            )
            return TrainingRunResult(release_path=release_path, metadata=metadata)
        finally:
            database.close()
    finally:
        temporary_directory.cleanup()


__all__ = [
    "LABELS",
    "TrainingConfig",
    "TrainingPipelineError",
    "TrainingRunResult",
    "run_training_pipeline",
]
