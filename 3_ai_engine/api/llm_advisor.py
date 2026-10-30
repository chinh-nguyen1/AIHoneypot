import os
import json
from dotenv import load_dotenv
import google.generativeai as genai

# Tải biến môi trường từ file .env ở thư mục gốc
load_dotenv()

# Lấy GEMINI_API_KEY
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

if GEMINI_API_KEY and GEMINI_API_KEY != "YOUR_GEMINI_API_KEY":
    genai.configure(api_key=GEMINI_API_KEY)

def analyze_and_advise_with_llm(payload: str) -> dict:
    """
    Gửi payload nghi vấn tới Gemini LLM để phân tích ngữ cảnh
    và tạo ra hướng dẫn khắc phục ngắn gọn cho Admin SOC.
    """
    # Nếu chưa có API Key hợp lệ -> Trả về kết quả mặc định an toàn
    if not GEMINI_API_KEY or GEMINI_API_KEY == "YOUR_GEMINI_API_KEY":
        return {
            "attack_type": "Suspicious_Payload",
            "confidence": 0.85,
            "risk_score": 8,
            "recommendation": "Phát hiện payload nghi vấn. Hãy kiểm tra và áp dụng cơ chế lọc dữ liệu đầu vào (Input Sanitization)."
        }

    prompt = f"""
    Bạn là một chuyên gia An ninh mạng SOC Senior. Hãy phân tích payload HTTP dưới đây từ hệ thống bẫy Web:

    Payload: "{payload}"

    Yêu cầu trả về chính xác định dạng JSON gồm các trường:
    1. "attack_type": Loại tấn công (SQLi, XSS, RCE, Path_Traversal, hoặc Suspicious).
    2. "confidence": Độ tự tin từ 0.0 đến 1.0.
    3. "risk_score": Điểm rủi ro từ 1 đến 10.
    4. "recommendation": Đúng 2 dòng bằng tiếng Việt hướng dẫn Admin cách vá lỗi triệt để trên hệ thống thật.

    Chỉ trả về JSON thuần túy, không có mã markdown hay lời giải thích thêm.
    """

    try:
        # Khởi tạo mô hình Gemini
        model = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json"}
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"[-] Lỗi gọi Gemini API: {e}")
        # Thử lại với tên mô hình fallback nếu gemini-1.5-flash báo lỗi 404
        try:
            model = genai.GenerativeModel('gemini-pro')
            response = model.generate_content(prompt)
            # Làm sạch chuỗi trả về nếu có markdown ```json
            clean_text = response.text.replace("```json", "").replace("```", "").strip()
            return json.loads(clean_text)
        except Exception as err:
            print(f"[-] Fallback Gemini API thất bại: {err}")
            return {
                "attack_type": "Suspicious_Obfuscated",
                "confidence": 0.80,
                "risk_score": 8,
                "recommendation": "Chuỗi mã hóa nghi vấn lẩn tránh. Cần kiểm tra kỹ các tham số mã hóa URL/Base64 và ép kiểu dữ liệu chặt chẽ."
            }