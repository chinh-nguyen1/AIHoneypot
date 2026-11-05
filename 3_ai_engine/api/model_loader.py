"""Load and validate a configured, versioned offline model release."""

import json
import platform
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import sklearn


SUPPORTED_SCHEMA_VERSIONS = frozenset({1})
CANONICAL_CLASSES = ("Normal", "SQLi", "XSS")
MODEL_FILENAME = "model.joblib"
TRANSFORMER_FILENAME = "feature_transform.joblib"
_CANDIDATE_ALGORITHMS = {
    "alternative": {
        "classifier": "SGDClassifier",
        "transformer": "HashingVectorizer",
    },
    "baseline": {
        "classifier": "RandomForestClassifier",
        "transformer": "TfidfVectorizer",
    },
}
_RELEASE_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ModelLoaderError(RuntimeError):
    """Raised when a configured model release cannot be safely loaded."""


@dataclass(frozen=True)
class LoadedModel:
    model: Any
    transformer: Any
    release_version: str
    candidate_name: str


def _validate_configuration(release_version: str, candidate_name: str) -> None:
    if (
        not isinstance(release_version, str)
        or not _RELEASE_VERSION_PATTERN.fullmatch(release_version)
        or ".." in release_version
        or "/" in release_version
        or "\\" in release_version
    ):
        raise ModelLoaderError("AI_MODEL_RELEASE is missing or is not a safe release name")
    if (
        not isinstance(candidate_name, str)
        or candidate_name not in _CANDIDATE_ALGORITHMS
    ):
        raise ModelLoaderError(
            "AI_MODEL_CANDIDATE must be 'alternative' or 'baseline'"
        )


def _resolve_within(path: Path, parent: Path, description: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(parent)
    except (OSError, ValueError) as exc:
        raise ModelLoaderError(
            f"{description} is missing or resolves outside its allowed directory"
        ) from exc
    return resolved


def _load_metadata(release_dir: Path, release_version: str) -> dict[str, Any]:
    metadata_path = _resolve_within(
        release_dir / "metadata.json", release_dir, "Release metadata"
    )
    if not metadata_path.is_file():
        raise ModelLoaderError("Release metadata is not a regular file")
    try:
        with metadata_path.open("r", encoding="utf-8") as stream:
            metadata = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelLoaderError("Release metadata is unreadable or invalid JSON") from exc

    if not isinstance(metadata, dict):
        raise ModelLoaderError("Release metadata must be a JSON object")
    if type(metadata.get("schema_version")) is not int:
        raise ModelLoaderError("Release metadata has no valid schema_version")
    if metadata["schema_version"] not in SUPPORTED_SCHEMA_VERSIONS:
        raise ModelLoaderError(
            f"Unsupported model metadata schema version: "
            f"{metadata['schema_version']}"
        )
    if metadata.get("release_version") != release_version:
        raise ModelLoaderError("Metadata release_version does not match configuration")
    if not isinstance(metadata.get("created_at_utc"), str) or not metadata[
        "created_at_utc"
    ]:
        raise ModelLoaderError("Release metadata has no valid created_at_utc")
    if not isinstance(metadata.get("preprocessing"), dict):
        raise ModelLoaderError("Release metadata has no valid preprocessing object")
    _validate_dependency_versions(metadata.get("library_versions"))
    if not isinstance(metadata.get("candidates"), dict):
        raise ModelLoaderError("Release metadata has no valid candidates object")
    return metadata


def _validate_dependency_versions(versions: Any) -> None:
    if not isinstance(versions, dict):
        raise ModelLoaderError("Release metadata has no valid library_versions object")
    required = ("python", "scikit_learn", "joblib", "numpy")
    if any(not isinstance(versions.get(name), str) or not versions[name] for name in required):
        raise ModelLoaderError("Release metadata is missing required library versions")

    saved_python = versions["python"].split(".")
    runtime_python = platform.python_version().split(".")
    if (
        len(saved_python) < 2
        or len(runtime_python) < 2
        or saved_python[:2] != runtime_python[:2]
    ):
        raise ModelLoaderError(
            f"Python version mismatch: release={versions['python']}, "
            f"runtime={platform.python_version()}"
        )

    runtime_versions = {
        "scikit_learn": sklearn.__version__,
        "joblib": joblib.__version__,
        "numpy": np.__version__,
    }
    for name, runtime_version in runtime_versions.items():
        if versions[name] != runtime_version:
            raise ModelLoaderError(
                f"{name} version mismatch: release={versions[name]}, "
                f"runtime={runtime_version}"
            )


def _validate_candidate_metadata(
    metadata: dict[str, Any], candidate_name: str
) -> dict[str, Any]:
    candidate = metadata["candidates"].get(candidate_name)
    if not isinstance(candidate, dict):
        raise ModelLoaderError(f"Candidate {candidate_name!r} is missing")
    if candidate.get("status") != "completed":
        raise ModelLoaderError(
            f"Candidate {candidate_name!r} is not completed"
        )
    if candidate.get("name") != candidate_name:
        raise ModelLoaderError("Candidate name does not match its metadata key")
    if candidate.get("model_filename") != MODEL_FILENAME:
        raise ModelLoaderError("Candidate model filename is not allowed")
    if candidate.get("transformer_filename") != TRANSFORMER_FILENAME:
        raise ModelLoaderError("Candidate transformer filename is not allowed")
    if candidate.get("canonical_classes") != list(CANONICAL_CLASSES):
        raise ModelLoaderError(
            "Candidate canonical_classes must be Normal, SQLi, XSS in order"
        )
    if candidate.get("algorithm") != _CANDIDATE_ALGORITHMS[candidate_name]:
        raise ModelLoaderError("Candidate algorithm metadata is invalid")
    if not isinstance(candidate.get("training_configuration"), dict):
        raise ModelLoaderError("Candidate training_configuration must be an object")
    if not isinstance(candidate.get("evaluation_metrics"), dict):
        raise ModelLoaderError("Candidate evaluation_metrics must be an object")
    return candidate


def _load_and_verify_artifacts(
    candidate_dir: Path, candidate: dict[str, Any]
) -> tuple[Any, Any]:
    model_path = _resolve_within(
        candidate_dir / candidate["model_filename"],
        candidate_dir,
        "Model artifact",
    )
    transformer_path = _resolve_within(
        candidate_dir / candidate["transformer_filename"],
        candidate_dir,
        "Transformer artifact",
    )
    if not model_path.is_file() or not transformer_path.is_file():
        raise ModelLoaderError("Model or transformer artifact is not a regular file")

    try:
        model = joblib.load(model_path)
        transformer = joblib.load(transformer_path)
    except Exception as exc:
        raise ModelLoaderError(
            f"Could not deserialize configured model artifacts: {type(exc).__name__}"
        ) from exc

    try:
        if not callable(getattr(transformer, "transform", None)):
            raise ModelLoaderError("Transformer does not expose transform()")
        if not callable(getattr(model, "predict", None)):
            raise ModelLoaderError("Model does not expose predict()")
        if not callable(getattr(model, "predict_proba", None)):
            raise ModelLoaderError("Model does not expose predict_proba()")
        if not hasattr(model, "classes_"):
            raise ModelLoaderError("Model does not expose classes_")

        model_classes = tuple(str(label) for label in model.classes_)
        if model_classes != CANONICAL_CLASSES:
            raise ModelLoaderError(
                "Loaded model classes_ do not match canonical_classes"
            )

        features = transformer.transform(["synthetic model loader contract payload"])
        if (
            not hasattr(features, "shape")
            or len(features.shape) != 2
            or features.shape[0] != 1
            or features.shape[1] < 1
        ):
            raise ModelLoaderError(
                "Transformer did not return a one-sample feature matrix"
            )
        predictions = np.asarray(model.predict(features))
        probabilities = np.asarray(model.predict_proba(features), dtype=float)
        if predictions.shape != (1,):
            raise ModelLoaderError("Model prediction output must have shape (1,)")
        if (
            not isinstance(predictions[0], (str, np.str_))
            or predictions[0] not in CANONICAL_CLASSES
        ):
            raise ModelLoaderError("Model predicted a label outside the taxonomy")
        if probabilities.shape != (1, len(CANONICAL_CLASSES)):
            raise ModelLoaderError(
                "Model probability output must have shape (1, 3)"
            )
        if (
            not np.isfinite(probabilities).all()
            or (probabilities < 0).any()
            or (probabilities > 1).any()
            or not np.isclose(
                probabilities.sum(), 1.0, rtol=1e-5, atol=1e-5
            )
        ):
            raise ModelLoaderError("Model returned invalid probabilities")
    except ModelLoaderError:
        raise
    except Exception as exc:
        raise ModelLoaderError(
            f"Model contract verification failed: {type(exc).__name__}"
        ) from exc
    return model, transformer


def load_model_release(
    release_root: Path | str,
    release_version: str,
    candidate_name: str,
) -> LoadedModel:
    """Load one explicitly selected and validated release candidate.

    Artifacts must come from a trusted source: joblib uses pickle-based
    deserialization and must not be used with untrusted files.
    """
    _validate_configuration(release_version, candidate_name)
    try:
        root = Path(release_root).resolve(strict=True)
    except (OSError, TypeError) as exc:
        raise ModelLoaderError("Configured model release root does not exist") from exc
    if not root.is_dir():
        raise ModelLoaderError("Configured model release root is not a directory")

    release_dir = _resolve_within(
        root / release_version, root, "Configured release directory"
    )
    if not release_dir.is_dir():
        raise ModelLoaderError("Configured release is not a directory")
    metadata = _load_metadata(release_dir, release_version)
    candidate = _validate_candidate_metadata(metadata, candidate_name)

    candidate_dir = _resolve_within(
        release_dir / candidate_name, release_dir, "Candidate directory"
    )
    if not candidate_dir.is_dir():
        raise ModelLoaderError("Configured candidate is not a directory")
    model, transformer = _load_and_verify_artifacts(candidate_dir, candidate)
    return LoadedModel(
        model=model,
        transformer=transformer,
        release_version=release_version,
        candidate_name=candidate_name,
    )
