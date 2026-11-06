import logging
import math
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware
import mysql.connector

from api.database_config import get_mysql_config
from api.model_loader import ModelLoaderError, load_model_release
from api.schemas import (
    AnalysisResponse,
    CANONICAL_ATTACK_TYPES,
    PayloadRequest,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="AI-WebHoneypot Core Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

AI_ENGINE_ROOT = Path(__file__).resolve().parents[1]
MODEL_RELEASE_ROOT = AI_ENGINE_ROOT / "artifacts" / "releases"
AI_MODEL_RELEASE = os.getenv("AI_MODEL_RELEASE", "")
AI_MODEL_CANDIDATE = os.getenv("AI_MODEL_CANDIDATE", "")
VALID_ATTACK_TYPES = frozenset(CANONICAL_ATTACK_TYPES)
LOCAL_MODEL_LABELS = VALID_ATTACK_TYPES
# Keep the existing attack scores; Normal uses the minimum score in the API contract.
LOCAL_RISK_SCORES = {"Normal": 1, "SQLi": 9, "XSS": 8}


LOCAL_CONFIDENCE_THRESHOLD = 0.85


def _load_local_model():
    try:
        loaded = load_model_release(
            MODEL_RELEASE_ROOT,
            AI_MODEL_RELEASE,
            AI_MODEL_CANDIDATE,
        )
    except ModelLoaderError as exc:
        logger.warning(
            "Local ML model is unavailable (%s)", type(exc).__name__
        )
        return None, None, type(exc).__name__
    except Exception as exc:
        logger.error(
            "Unexpected error loading configured Local ML model (%s)",
            type(exc).__name__,
        )
        return None, None, type(exc).__name__
    return loaded.model, loaded.transformer, None


model, vectorizer, LOCAL_MODEL_LOAD_ERROR = _load_local_model()

def _predict_local(payload: str):
    if model is None or vectorizer is None:
        return {
            "source": "Local_ML",
            "classification_status": "NEEDS_REVIEW",
            "attack_type": None,
            "confidence": None,
            "risk_score": None,
            "reason": "MODEL_UNAVAILABLE",
            "recommendation": None,
        }

    try:
        features = vectorizer.transform([payload])
        predicted = model.predict(features)
        probabilities = model.predict_proba(features)
        classes = list(model.classes_)
        if len(predicted) != 1 or len(probabilities) != 1:
            raise ValueError("model returned an unexpected prediction shape")

        attack_type = str(predicted[0])
        if not isinstance(attack_type, str) or attack_type not in LOCAL_MODEL_LABELS:
            raise ValueError("model returned an unsupported classification label")
        if attack_type not in classes:
            raise ValueError("model prediction is missing from its class list")

        probability_row = probabilities[0]
        if len(probability_row) != len(classes):
            raise ValueError("model probability count does not match its classes")
        probability_values = []
        for value in probability_row:
            if isinstance(value, bool):
                raise ValueError("model returned a boolean confidence score")
            probability = float(value)
            if not math.isfinite(probability) or not 0 <= probability <= 1:
                raise ValueError("model returned an invalid confidence score")
            probability_values.append(probability)
        if not math.isclose(
            sum(probability_values), 1.0, rel_tol=1e-5, abs_tol=1e-5
        ):
            raise ValueError("model probabilities do not sum to one")
        confidence = probability_values[classes.index(attack_type)]
    except Exception as exc:
        logger.warning("Local ML inference failed (%s)", type(exc).__name__)
        return {
            "source": "Local_ML",
            "classification_status": "NEEDS_REVIEW",
            "attack_type": None,
            "confidence": None,
            "risk_score": None,
            "reason": "INFERENCE_ERROR",
            "recommendation": None,
        }

    if confidence < LOCAL_CONFIDENCE_THRESHOLD:
        logger.info(
            "Local ML confidence %.4f is below threshold %.4f",
            confidence,
            LOCAL_CONFIDENCE_THRESHOLD,
        )
        return {
            "source": "Local_ML",
            "classification_status": "NEEDS_REVIEW",
            "attack_type": None,
            "confidence": confidence,
            "risk_score": None,
            "reason": "LOW_CONFIDENCE",
            "recommendation": None,
        }

    result = {
        "source": "Local_ML",
        "classification_status": "CLASSIFIED",
        "attack_type": attack_type,
        "confidence": confidence,
        "risk_score": LOCAL_RISK_SCORES[attack_type],
        "reason": None,
        "recommendation": None,
    }
    if attack_type == "SQLi":
        result["recommendation"] = (
            "Review SQL query construction and use parameterized statements."
        )
    elif attack_type == "XSS":
        result["recommendation"] = (
            "Review input handling and apply context-appropriate output encoding."
        )
    return result


@app.get("/stats")
def get_stats():
    connection = None
    cursor = None
    error = None
    result = None
    try:
        connection = mysql.connector.connect(**get_mysql_config())
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT COUNT(*) AS total_logs, "
            "COUNT(DISTINCT client_ip) AS unique_ips FROM raw_logs"
        )
        totals = cursor.fetchone()
        if not isinstance(totals, dict):
            raise RuntimeError("database returned invalid total statistics")

        cursor.execute(
            """
            SELECT latest.attack_type, COUNT(*) AS event_count
            FROM raw_logs r
            JOIN ai_labels latest ON latest.id = (
                SELECT a2.id
                FROM ai_labels a2
                WHERE a2.log_id = r.id
                ORDER BY a2.analyzed_at DESC, a2.id DESC
                LIMIT 1
            )
            WHERE latest.classification_status = 'CLASSIFIED'
            GROUP BY latest.attack_type
            """
        )
        attack_counts = {
            row["attack_type"]: row["event_count"]
            for row in cursor.fetchall()
        }

        cursor.execute(
            """
            SELECT COUNT(*) AS unclassified_count
            FROM raw_logs r
            WHERE NOT EXISTS (
                SELECT 1 FROM ai_labels a WHERE a.log_id = r.id AND a.classification_status = 'CLASSIFIED'
            )
            """
        )
        unclassified = cursor.fetchone()
        if not isinstance(unclassified, dict):
            raise RuntimeError("database returned invalid unclassified statistics")

        result = {
            "total_logs": totals["total_logs"],
            "classified_count": sum(attack_counts.values()),
            "unclassified_count": unclassified["unclassified_count"],
            "attack_counts": attack_counts,
            "sqli_count": attack_counts.get("SQLi", 0),
            "xss_count": attack_counts.get("XSS", 0),
            "normal_count": attack_counts.get("Normal", 0),
            "unique_ips": totals["unique_ips"],
        }
    except Exception as exc:
        error = exc

    for resource in (cursor, connection):
        if resource is not None:
            try:
                resource.close()
            except Exception as exc:
                if error is None:
                    error = exc

    if error is not None:
        logger.error(
            "Dashboard statistics query failed",
            exc_info=(type(error), error, error.__traceback__),
        )
        raise HTTPException(
            status_code=503,
            detail="Dashboard statistics are temporarily unavailable",
        ) from error
    return result

@app.get("/logs/live")
def get_live_logs():
    connection = None
    cursor = None
    error = None
    rows = None
    try:
        connection = mysql.connector.connect(**get_mysql_config())
        cursor = connection.cursor(dictionary=True)
        query = """
            SELECT CAST(r.id AS CHAR) AS id,
                   r.created_at, r.client_ip, r.http_method, r.request_uri,
                   a.attack_type, a.confidence_score, a.risk_score,
                   CASE
                       WHEN a.id IS NULL OR a.classification_status <> 'CLASSIFIED' THEN 'UNCLASSIFIED'
                       ELSE 'CLASSIFIED'
                   END AS classification_status,
                   a.recommendation AS recommendation
            FROM raw_logs r
            LEFT JOIN ai_labels a ON a.id = (
                SELECT a2.id
                FROM ai_labels a2
                WHERE a2.log_id = r.id
                ORDER BY a2.analyzed_at DESC, a2.id DESC
                LIMIT 1
            )
            ORDER BY r.id DESC
            LIMIT 10
        """
        cursor.execute(query)
        rows = cursor.fetchall()
    except Exception as exc:
        error = exc

    for resource in (cursor, connection):
        if resource is not None:
            try:
                resource.close()
            except Exception as exc:
                if error is None:
                    error = exc

    if error is not None:
        logger.error(
            "Live log query failed",
            exc_info=(type(error), error, error.__traceback__),
        )
        raise HTTPException(
            status_code=503,
            detail="Live events are temporarily unavailable",
        ) from error
    return rows

@app.post("/analyze", response_model=AnalysisResponse)
def analyze(data: PayloadRequest):
    return _predict_local(data.payload)
