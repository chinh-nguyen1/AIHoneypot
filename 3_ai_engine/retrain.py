from pathlib import Path

import pandas as pd
import joblib
import mysql.connector
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.ensemble import RandomForestClassifier
from api.database_config import get_mysql_config


AI_ENGINE_ROOT = Path(__file__).resolve().parent
MODEL_PATH = AI_ENGINE_ROOT / "models" / "model_rf.pkl"
VECTORIZER_PATH = AI_ENGINE_ROOT / "models" / "vectorizer.pkl"
MIN_TRAINING_ROWS = 10
TRAINING_LABELS = frozenset({"SQLi", "XSS", "Normal"})


def fetch_data_from_db():
    """Trích xuất dữ liệu log thô và nhãn phân loại từ MySQL"""
    connection = None
    try:
        connection = mysql.connector.connect(**get_mysql_config())
        query = """
            SELECT r.decoded_payload AS payload, a.attack_type AS label 
            FROM raw_logs r 
            JOIN ai_labels a ON r.id = a.log_id 
            WHERE a.attack_type IN ('SQLi', 'XSS', 'Normal')
        """
        return pd.read_sql(query, connection)
    except Exception as exc:
        raise RuntimeError("Could not retrieve retraining data from MySQL") from exc
    finally:
        if connection is not None:
            connection.close()


def _validate_training_data(df):
    if df.empty or len(df) < MIN_TRAINING_ROWS:
        raise ValueError(
            f"At least {MIN_TRAINING_ROWS} labeled database rows are required; "
            f"received {len(df)}"
        )
    if not {"payload", "label"}.issubset(df.columns):
        raise ValueError("Training data must include payload and label columns")

    payloads = list(df["payload"])
    labels = list(df["label"])
    if any(not isinstance(payload, str) or not payload.strip() for payload in payloads):
        raise ValueError("Training payloads must be non-empty strings")
    if any(not isinstance(label, str) or label not in TRAINING_LABELS for label in labels):
        raise ValueError("Training data contains an unsupported classification label")
    if len(set(labels)) < 2:
        raise ValueError("Training data must contain at least two distinct labels")
    return payloads, labels


def _save_new_artifacts(model, vectorizer):
    """Create both artifact files without replacing any pre-existing file."""
    created_paths = []
    try:
        with MODEL_PATH.open("xb") as model_file:
            created_paths.append(MODEL_PATH)
            joblib.dump(model, model_file)
        with VECTORIZER_PATH.open("xb") as vectorizer_file:
            created_paths.append(VECTORIZER_PATH)
            joblib.dump(vectorizer, vectorizer_file)
    except Exception:
        for path in created_paths:
            if path.exists():
                path.unlink()
        raise

def retrain_model():
    print("[*] Đang khởi chạy quá trình tái huấn luyện mô hình Local ML...")
    existing_artifacts = [
        path for path in (MODEL_PATH, VECTORIZER_PATH) if path.exists()
    ]
    if existing_artifacts:
        raise FileExistsError(
            "Refusing to overwrite existing model artifact(s): "
            + ", ".join(path.name for path in existing_artifacts)
        )

    df = fetch_data_from_db()
    payloads, labels = _validate_training_data(df)
    vectorizer = TfidfVectorizer(ngram_range=(1, 3), analyzer='char')
    X = vectorizer.fit_transform(payloads)

    model = RandomForestClassifier(n_estimators=100, random_state=42)
    model.fit(X, labels)

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    VECTORIZER_PATH.parent.mkdir(parents=True, exist_ok=True)
    _save_new_artifacts(model, vectorizer)
    print(
        f"[+] [Retrain] Huấn luyện thành công trên {len(df)} mẫu. "
        "Đã tạo model_rf.pkl và vectorizer.pkl."
    )

if __name__ == "__main__":
    retrain_model()