from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import joblib
import numpy as np
import mysql.connector

# IMPORT MODULE GEMINI LLM
from api.llm_advisor import analyze_and_advise_with_llm

app = FastAPI(title="AI-WebHoneypot Core Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Tải model Local ML
try:
    model = joblib.load("models/model_rf.pkl")
    vectorizer = joblib.load("models/vectorizer.pkl")
except Exception:
    model, vectorizer = None, None

class PayloadRequest(BaseModel):
    payload: str

MYSQL_CONFIG = {
    'host': 'database',
    'user': 'root',
    'password': 'rootpassword',
    'database': 'ai_webhoneypot_db'
}

@app.get("/stats")
def get_stats():
    try:
        conn = mysql.connector.connect(**MYSQL_CONFIG)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT COUNT(*) AS total_logs FROM raw_logs")
        total = cursor.fetchone()['total_logs']
        cursor.execute("SELECT COUNT(*) AS sqli FROM ai_labels WHERE attack_type = 'SQLi'")
        sqli = cursor.fetchone()['sqli']
        cursor.execute("SELECT COUNT(*) AS xss FROM ai_labels WHERE attack_type = 'XSS'")
        xss = cursor.fetchone()['xss']
        cursor.execute("SELECT COUNT(DISTINCT client_ip) AS unique_ips FROM raw_logs")
        ips = cursor.fetchone()['unique_ips']
        conn.close()
        return {"total_logs": total, "sqli_count": sqli, "xss_count": xss, "normal_count": max(0, total - sqli - xss), "unique_ips": ips}
    except Exception:
        return {"total_logs": 0, "sqli_count": 0, "xss_count": 0, "normal_count": 0, "unique_ips": 0}

@app.get("/logs/live")
def get_live_logs():
    try:
        conn = mysql.connector.connect(**MYSQL_CONFIG)
        cursor = conn.cursor(dictionary=True)
        query = """
            SELECT r.client_ip, r.http_method, r.request_uri, a.attack_type, a.risk_score, r.created_at 
            FROM raw_logs r 
            JOIN ai_labels a ON r.id = a.log_id 
            ORDER BY r.id DESC LIMIT 10
        """
        cursor.execute(query)
        rows = cursor.fetchall()
        conn.close()
        return rows
    except Exception:
        return []

@app.post("/analyze")
def analyze(data: PayloadRequest):
    payload = data.payload
    
    # ----------------------------------------------------
    # TẦNG 1: Kiểm tra bằng Local ML (Tốc độ < 10ms)
    # ----------------------------------------------------
    if model and vectorizer:
        X_vec = vectorizer.transform([payload])
        probs = model.predict_proba(X_vec)
        max_prob = float(np.max(probs))
        predicted_class = model.predict(X_vec)[0]
        
        # Nếu Local ML tự tin >= 85%
        if max_prob >= 0.85 and predicted_class != 'Normal':
            return {
                "source": "Local_ML",
                "attack_type": predicted_class,
                "confidence": round(max_prob, 2),
                "risk_score": 9 if predicted_class == 'SQLi' else 8,
                "recommendation": f"Cảnh báo {predicted_class}: Hãy áp dụng cơ chế Input Sanitization hoặc Prepared Statements."
            }

    # ----------------------------------------------------
    # TẦNG 2: Gọi Gemini LLM API (Phân tích Zero-day & sinh lời khuyên)
    # ----------------------------------------------------
    llm_result = analyze_and_advise_with_llm(payload)
    return {
        "source": "Cloud_Gemini_LLM",
        "attack_type": llm_result.get("attack_type", "Suspicious"),
        "confidence": llm_result.get("confidence", 0.85),
        "risk_score": llm_result.get("risk_score", 8),
        "recommendation": llm_result.get("recommendation", "Phát hiện chuỗi mã hóa nghi vấn lẩn tránh.")
    }
