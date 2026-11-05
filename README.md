Dưới đây là nội dung hoàn chỉnh, chuẩn mực và chi tiết cho file **`README.md`** đặt tại thư mục gốc `ai-webhoneypot/`.

Bạn chỉ cần tạo tệp **`README.md`** ở thư mục gốc trên VS Code và dán toàn bộ đoạn mã bên dưới vào:

````markdown
# 🛡️ AI-WEBHONEYPOT: HỆ THỐNG BẪY WEB VÀ PHÂN TÍCH TẤN CÔNG THỜI GIAN THỰC TÍCH HỢP HYBRID AI

> **Đồ án tốt Nghiệp / Capstone Project**  
> **Đề tài:** Xây dựng hệ thống bẫy Web mồi nhử tích hợp AI 2 tầng (Local ML + Cloud Gemini LLM Fallback), phân tích mối đe dọa thời gian thực và tự động gửi cảnh báo khẩn cấp qua Email.

---

## 📌 1. TỔNG QUAN HỆ THỐNG

**AI-WebHoneypot** là giải pháp an ninh mạng chủ động. Hệ thống đóng vai trò như một cửa hàng thương mại điện tử thật (**TechStore**) chứa các lỗ hổng mồi nhử (SQL Injection, Reflected XSS, Stored XSS) để dụ kẻ tấn công (Hacker).

Khi Hacker tiến hành khai thác:

1. **Web Bẫy** tự động ghi nhận log thô.
2. **Log Pipeline** giải mã các kỹ thuật lẩn tránh (URL Encode, Base64).
3. **Lõi AI 2 Tầng (Hybrid AI)** phân loại tấn công:
   - **Tầng 1 (Local ML - Random Forest):** Phân loại siêu tốc (< 10ms) với các mẫu tấn công đã biết.
   - **Tầng 2 (Cloud Gemini LLM):** Phân tích ngữ cảnh sâu các chuỗi lắt léo/Zero-day và tự động sinh 2 dòng đề xuất vá lỗi bằng tiếng Việt.
4. **Cảnh báo khẩn (Email Alert):** Tự động gửi Email báo động đỏ tới Admin SOC khi điểm rủi ro \\(\ge 8/10\\).
5. **SOC Admin Dashboard (ReactJS):** Hiển thị thống kê, phân loại sự kiện và luồng log; bản đồ chỉ hiển thị khi có dữ liệu vị trí đã xác minh.
6. **Đóng gói Docker:** 100% hệ thống vận hành cô lập trong 6 Docker Container (không cần cài XAMPP).

---

## 📂 2. CẤU TRÚC THƯ MỤC DỰ ÁN

```text
ai-webhoneypot/
├── docker-compose.yml          # File điều khiển & khởi chạy toàn bộ 6 container
├── Dockerfile.decoy            # Dockerfile cho Web Bẫy (PHP 8.2 + Apache)
├── Dockerfile.pipeline         # Dockerfile cho Log Pipeline & Email Alert (Python)
├── Dockerfile.ai               # Dockerfile cho Lõi AI 2 Tầng (FastAPI)
├── Dockerfile.database         # Dockerfile cho CSDL Trung tâm (MySQL 8.0)
├── Dockerfile.dashboard        # Dockerfile cho SOC Admin Dashboard (ReactJS + Nginx)
├── Dockerfile.attacker         # Dockerfile cho Attacker Console (Python + sqlmap)
├── .env                        # Chứa API Key Gemini & Cấu hình Email SMTP
├── .gitignore                  # Bỏ qua file rác, .env, log thô khỏi Git
├── README.md                   # Tài liệu hướng dẫn hệ thống
│
├── 1_decoy_web/                # Web Bẫy E-Commerce TechStore (PHP/MySQL)
│   ├── config/db.php           # Kết nối CSDL MySQL
│   ├── public/                 # Các trang bẫy (index, login, search, cart, comment)
│   └── logs/access.log         # File lưu log thô
│
├── 2_log_pipeline/             # Đường ống xử lý Log & Cảnh báo Email
│   ├── log_watcher.py          # Script ngầm bắt log sự kiện
│   ├── decoder.py              # Giải mã URL/Base64
│   ├── email_alert.py          # Gửi email cảnh báo qua SMTP
│   └── pipeline_main.py        # Luồng chính xử lý dữ liệu
│
├── 3_ai_engine/                # Lõi AI 2 Tầng (FastAPI + ML + Gemini)
│   ├── api/main.py             # FastAPI Service (/analyze, /stats, /logs/live)
│   ├── api/llm_advisor.py      # Tích hợp Gemini LLM API
│   ├── models/train_model.py   # Huấn luyện Random Forest
│   └── retrain.py              # Re-train mô hình định kỳ
│
├── 4_database/                 # CSDL MySQL Trung tâm
│   ├── schema.sql              # Khởi tạo 4 bảng CSDL
│   └── seed_data.sql           # Dữ liệu mẫu ban đầu
│
├── 5_frontend_dashboard/       # SOC Admin Dashboard (ReactJS)
│   └── src/                    # Components (Overview, Chart, Map, LiveLogs)
│
└── 6_security_pentest/         # Mô-đun Giả Lập Tấn Công (Attacker Console)
    └── attacker_console/       # Giao diện bấm nút bắn đạn demo & script pentest
```
````

---

## ⚙️ 3. YÊU CẦU TIỀN TRÌNH & CÀI ĐẶT

### Yêu cầu phần mềm:

- **Docker Desktop** (Đã bật WSL2 Backend trên Windows).
- **VS Code** (Khuyên dùng kèm extension Docker).
- **Git**.

### Bước 1: Cấu hình Tệp `.env`

Tạo file `.env` tại thư mục gốc `ai-webhoneypot/` và điền các thông số:

```env
# Gemini API Key (Lấy tại https://aistudio.google.com/app/apikey)
GEMINI_API_KEY=your_gemini_api_key_here

# Cấu hình Email gửi cảnh báo (Gmail)
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SENDER_EMAIL=your_email@gmail.com
SENDER_PASSWORD=your_16_digit_app_password
RECEIVER_EMAIL=admin_soc@gmail.com
```

---

## 🚀 4. HƯỚNG DẪN KHỞI CHẠY (QUICK START)

1. Mở ứng dụng **Docker Desktop**.
2. Mở **Terminal** trong VS Code tại thư mục gốc `ai-webhoneypot/`.
3. Chạy câu lệnh duy nhất để build và kích hoạt toàn bộ 6 container:

```bash
docker compose up --build -d
```

4. Kiểm tra trạng thái các container đang chạy:

```bash
docker compose ps
```

---

## 🌐 5. DANH SÁCH CỔNG TRUY CẬP DỊCH VỤ

| Dịch vụ                 | Công nghệ         | URL Truy cập                 |
| :---------------------- | :---------------- | :--------------------------- |
| **Web Bẫy TechStore**   | PHP 8.2 / Apache  | `http://localhost:8080`      |
| **SOC Admin Dashboard** | ReactJS / Nginx   | `http://localhost:3000`      |
| **Attacker Console**    | Python / Pentest  | `http://localhost:8081`      |
| **Lõi AI FastAPI Docs** | FastAPI / Swagger | `http://localhost:8000/docs` |
| **Cơ sở dữ liệu MySQL** | MySQL 8.0         | `localhost:3306`             |

Dashboard mặc định gọi AI Engine tại `http://localhost:8000`. Khi build frontend
cho môi trường khác, cấu hình build argument `REACT_APP_API_BASE_URL` thành URL
API mà trình duyệt người dùng có thể truy cập.

---

## 🧪 6. QUY TRÌNH KIỂM THỬ DEMO (TESTING WORKFLOW)

1. **Bước 1:** Truy cập Web Bẫy `http://localhost:8080/public/login.php`.
2. **Bước 2:** Nhập chuỗi SQL Injection vào ô Username:
   ```text
   admin' OR 1=1 --
   ```
   và nhấn **Đăng nhập**.
3. **Bước 3:** Kiểm tra kết quả phản ứng hệ thống:
   - **Log Pipeline:** Bắt dòng log thô và giải mã URL.
   - **AI Engine:** Phân loại nhãn `SQLi` với điểm rủi ro `9/10`.
   - **Email Alert:** Tự động bắn 1 Email báo động đỏ về hòm thư `RECEIVER_EMAIL`.
   - **SOC Dashboard (`http://localhost:3000`):** Tự động nhảy số thống kê, hiển thị log mới lên bảng và đưa ra lời khuyên vá lỗi.

---

## 🛑 7. TẮT VÀ DỌN DẸP MÔI TRƯỜNG

Để dừng toàn bộ hệ thống container:

```bash
docker compose stop
```

Nếu muốn xóa toàn bộ dữ liệu container cũ để build lại từ đầu:

```bash
docker compose down -v
```

_Dự án được xây dựng và phát triển bởi Nhóm Đồ án Capstone AI-WebHoneypot._
