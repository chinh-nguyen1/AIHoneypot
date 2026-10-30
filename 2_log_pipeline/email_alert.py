import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from config import SMTP_SERVER, SMTP_PORT, SENDER_EMAIL, SENDER_PASSWORD, RECEIVER_EMAIL

def send_email_alert(client_ip, attack_type, risk_score, payload):
    """Gửi email cảnh báo khẩn cấp khi phát hiện tấn công có nguy cơ cao"""
    if not SENDER_EMAIL or SENDER_EMAIL == "your_email@gmail.com":
        print("[-] Bỏ qua gửi Email: Chưa cấu hình SENDER_EMAIL trong config.py")
        return

    subject = f"🚨 [AI-WEBHONEYPOT ALERT] Phát hiện tấn công {attack_type} (Risk: {risk_score}/10)"
    
    # Nội dung Email dạng HTML định dạng đẹp
    body_html = f"""
    <html>
    <body style="font-family: Arial, sans-serif; background-color: #f8f9fa; padding: 20px;">
        <div style="max-width: 600px; margin: 0 auto; background: #ffffff; border-left: 6px solid #dc3545; padding: 20px; border-radius: 4px; box-shadow: 0 2px 5px rgba(0,0,0,0.1);">
            <h2 style="color: #dc3545; margin-top: 0;">🚨 CẢNH BÁO TẤN CÔNG NGUY HIỂM</h2>
            <hr style="border: none; border-top: 1px solid #eee;">
            <p><b>🌐 Địa chỉ IP Hacker:</b> <span style="background: #e9ecef; padding: 2px 6px; border-radius: 3px; font-family: monospace;">{client_ip}</span></p>
            <p><b>⚔️ Loại hình tấn công:</b> <span style="color: #d9534f; font-weight: bold;">{attack_type}</span></p>
            <p><b>🔥 Điểm rủi ro (Risk Score):</b> <span style="color: #d9534f; font-weight: bold;">{risk_score}/10</span></p>
            <p><b>💣 Payload độc hại:</b></p>
            <pre style="background: #212529; color: #00ff00; padding: 12px; border-radius: 4px; overflow-x: auto;">{payload}</pre>
            <hr style="border: none; border-top: 1px solid #eee;">
            <p style="font-size: 12px; color: #6c757d; margin-bottom: 0;"><i>Hệ thống AI-WebHoneypot đã tự động ghi nhận vết và lưu vào cơ sở dữ liệu.</i></p>
        </div>
    </body>
    </html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECEIVER_EMAIL
    msg.attach(MIMEText(body_html, "html"))

    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, RECEIVER_EMAIL, msg.as_string())
        server.quit()
        print(f"[+] Đã gửi email cảnh báo tới {RECEIVER_EMAIL} thành công!")
    except Exception as e:
        print(f"[-] Lỗi gửi Email Alert: {e}")
