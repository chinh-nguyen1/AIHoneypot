# 2_log_pipeline/config.py

MYSQL_CONFIG = {
    'host': 'localhost',
    'user': 'root',
    'password': '',
    'database': 'ai_webhoneypot_db'
}

AI_API_URL = "http://127.0.0.1:8000/analyze"

# Cấu hình Email SMTP (Ví dụ dùng Gmail)
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = "your_email@gmail.com"
SENDER_PASSWORD = "your_app_password"  # Mật khẩu ứng dụng (App Password) từ Google
RECEIVER_EMAIL = "admin_soc@gmail.com"  # Email của Admin nhận cảnh báo