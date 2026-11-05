import os
import json
from dotenv import load_dotenv
import google.generativeai as genai

# Tải biến môi trường từ file .env ở thư mục gốc
load_dotenv()

# Lấy GEMINI_API_KEY
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")


class LLMInvalidResponseError(ValueError):
    """Raised when Gemini returns JSON that is malformed or not an object."""


if GEMINI_API_KEY and GEMINI_API_KEY != "YOUR_GEMINI_API_KEY":
    genai.configure(api_key=GEMINI_API_KEY)


def _parse_llm_response(response_text: str) -> dict:
    try:
        result = json.loads(response_text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LLMInvalidResponseError("Gemini returned invalid JSON") from exc

    if not isinstance(result, dict):
        raise LLMInvalidResponseError("Gemini JSON response must be an object")
    return result


def analyze_and_advise_with_llm(payload: str) -> dict:
    """
    Gửi payload nghi vấn tới Gemini LLM để phân tích ngữ cảnh
    và tạo ra hướng dẫn khắc phục ngắn gọn cho Admin SOC.
    """
    # Missing credentials are an unavailable service, not a classification result.
    if not GEMINI_API_KEY or GEMINI_API_KEY == "YOUR_GEMINI_API_KEY":
        raise RuntimeError("Gemini API key is not configured")

    prompt = f"""
    Bạn là một chuyên gia An ninh mạng SOC Senior. Hãy phân tích payload HTTP dưới đây từ hệ thống bẫy Web:

    Payload: "{payload}"

    Yêu cầu trả về chính xác định dạng JSON gồm các trường:
    1. "attack_type": Nhãn phân loại, chỉ được là Normal, SQLi hoặc XSS.
    2. "confidence": Độ tự tin từ 0.0 đến 1.0.
    3. "risk_score": Điểm rủi ro từ 1 đến 10.
    4. "recommendation": Đúng 2 dòng bằng tiếng Việt hướng dẫn Admin cách vá lỗi triệt để trên hệ thống thật.

    Chỉ trả về JSON thuần túy, không có mã markdown hay lời giải thích thêm.
    """

    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json"}
        )
    except Exception as e:
        print(f"[-] Lỗi gọi Gemini API: {e}")
        try:
            model = genai.GenerativeModel('gemini-pro')
            response = model.generate_content(prompt)
            clean_text = response.text.replace("```json", "").replace("```", "").strip()
            return _parse_llm_response(clean_text)
        except LLMInvalidResponseError:
            raise
        except Exception as err:
            print(f"[-] Fallback Gemini API thất bại: {err}")
            raise RuntimeError("Gemini analysis is unavailable") from err
    return _parse_llm_response(response.text)