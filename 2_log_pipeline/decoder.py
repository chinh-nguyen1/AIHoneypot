import urllib.parse
import base64

def decode_payload(raw_string: str) -> str:
    """Giải mã URL Encode và Base64 lẩn tránh"""
    decoded = urllib.parse.unquote(raw_string)
    try:
        if len(decoded) % 4 == 0 and decoded.isalnum():
            b64_decoded = base64.b64decode(decoded).decode('utf-8', errors='ignore')
            if len(b64_decoded) > 0:
                return b64_decoded
    except Exception:
        pass
    return decoded