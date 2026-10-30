import pandas as pd
import joblib
import mysql.connector
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.ensemble import RandomForestClassifier

MYSQL_CONFIG = {
    'host': 'localhost',
    'user': 'root',
    'password': '',
    'database': 'ai_webhoneypot_db'
}

def fetch_data_from_db():
    """Trích xuất dữ liệu log thô và nhãn phân loại từ MySQL"""
    try:
        conn = mysql.connector.connect(**MYSQL_CONFIG)
        query = """
            SELECT r.decoded_payload AS payload, a.attack_type AS label 
            FROM raw_logs r 
            JOIN ai_labels a ON r.id = a.log_id 
            WHERE a.attack_type IN ('SQLi', 'XSS', 'Normal')
        """
        df = pd.read_sql(query, conn)
        conn.close()
        return df
    except Exception as e:
        print(f"[-] Lỗi kết nối CSDL khi Re-train: {e}")
        return pd.DataFrame()

def retrain_model():
    print("[*] Đang khởi chạy quá trình tái huấn luyện mô hình Local ML...")
    df = fetch_data_from_db()
    
    if df.empty or len(df) < 10:
        print("[-] CSDL chưa đủ 10 mẫu dữ liệu. Nạp tập dữ liệu khởi tạo mặc định.")
        data = {
            'payload': [
                "admin' OR '1'='1", "' OR 1=1 --", "SELECT * FROM users", "UNION SELECT null, pass FROM users",
                "<script>alert(1)</script>", "<img src=x onerror=alert('XSS')>", "<svg onload=alert(1)>",
                "GET /index.php", "POST /login.php", "dienthoai iphone 15", "search laptop dell"
            ],
            'label': ['SQLi', 'SQLi', 'SQLi', 'SQLi', 'XSS', 'XSS', 'XSS', 'Normal', 'Normal', 'Normal', 'Normal']
        }
        df = pd.DataFrame(data)

    vectorizer = TfidfVectorizer(ngram_range=(1, 3), analyzer='char')
    X = vectorizer.fit_transform(df['payload'])
    y = df['label']

    model = RandomForestClassifier(n_estimators=100, random_state=42)
    model.fit(X, y)

    joblib.dump(model, 'models/model_rf.pkl')
    joblib.dump(vectorizer, 'models/vectorizer.pkl')
    print(f"[+] [Retrain] Tái huấn luyện thành công trên {len(df)} mẫu. Đã cập nhật model_rf.pkl và vectorizer.pkl!")

if __name__ == "__main__":
    retrain_model()