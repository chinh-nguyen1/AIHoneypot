import time
import re
import requests
import mysql.connector
from decoder import decode_payload
from email_alert import send_email_alert
from config import MYSQL_CONFIG, AI_API_URL

LOG_FILE = "../1_decoy_web/logs/access.log"

def process_line(line):
    pattern = r"\\[(.*?)\\] IP: (.*?) \| Method: (.*?) \| URI: (.*?) \| Payload: (.*)"
    match = re.search(pattern, line)
    
    if match:
        timestamp, client_ip, method, uri, raw_payload = match.groups()
        decoded = decode_payload(raw_payload)
        
        # 1. Gọi AI Engine phân loại
        attack_type, confidence, risk_score = "Normal", 0.99, 1
        try:
            res = requests.post(AI_API_URL, json={"payload": decoded}, timeout=2)
            if res.status_code == 200:
                data = res.json()
                attack_type = data.get("attack_type", "Normal")
                confidence = data.get("confidence", 0.99)
                risk_score = data.get("risk_score", 1)
        except Exception:
            pass

        print(f"[+] [{attack_type}] Bắt log từ IP {client_ip} | Risk: {risk_score}")

        # 2. Bắn cảnh báo Telegram nếu Risk Score >= 8
        if risk_score >= 8:
            send_email_alert(client_ip, attack_type, risk_score, decoded)

        # 3. Lưu dữ liệu vào MySQL
        try:
            conn = mysql.connector.connect(**MYSQL_CONFIG)
            cursor = conn.cursor()
            
            # Insert raw_logs
            sql_log = "INSERT INTO raw_logs (client_ip, http_method, request_uri, raw_payload, decoded_payload) VALUES (%s, %s, %s, %s, %s)"
            cursor.execute(sql_log, (client_ip, method, uri, raw_payload, decoded))
            log_id = cursor.lastrowid
            
            # Insert ai_labels
            sql_ai = "INSERT INTO ai_labels (log_id, attack_type, confidence_score, risk_score) VALUES (%s, %s, %s, %s)"
            cursor.execute(sql_ai, (log_id, attack_type, confidence, risk_score))
            
            # Nếu nguy hiểm -> Đẩy vào pentest_payloads
            if risk_score >= 8:
                sql_pentest = "INSERT INTO pentest_payloads (log_id, attack_type, high_risk_payload) VALUES (%s, %s, %s)"
                cursor.execute(sql_pentest, (log_id, attack_type, decoded))

            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[-] Lỗi lưu CSDL: {e}")

if __name__ == "__main__":
    print("[*] Log Pipeline đang theo dõi file access.log...")
    open(LOG_FILE, "a").close() # Tạo file nếu chưa có
    with open(LOG_FILE, "r") as f:
        f.seek(0, 2)
        while True:
            line = f.readline()
            if not line:
                time.sleep(1)
                continue
            process_line(line)