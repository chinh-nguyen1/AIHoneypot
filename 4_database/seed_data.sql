USE ai_webhoneypot_db;

-- Làm sạch dữ liệu cũ trước khi nạp dữ liệu mẫu
SET FOREIGN_KEY_CHECKS = 0;
TRUNCATE TABLE pentest_payloads;
TRUNCATE TABLE ai_labels;
TRUNCATE TABLE raw_logs;
TRUNCATE TABLE threat_ips;
SET FOREIGN_KEY_CHECKS = 1;

-- 1. Nạp dữ liệu mẫu cho bảng threat_ips (Tọa độ địa lý hiển thị trên Bản đồ SOC)
INSERT INTO threat_ips (ip_address, country_name, latitude, longitude, total_attacks) VALUES
('113.161.72.11', 'Vietnam (TP.HCM)', 10.82310000, 106.62970000, 15),
('103.21.244.5', 'Vietnam (Hà Nội)', 21.02850000, 105.85420000, 8),
('185.220.101.5', 'Germany (Frankfurt)', 51.16570000, 10.45150000, 24),
('45.155.205.233', 'Russia (Moscow)', 55.75580000, 37.61730000, 42),
('192.168.1.105', 'Localhost Network', 10.76262200, 106.66017200, 5);

-- 2. Nạp dữ liệu mẫu cho raw_logs & ai_labels
INSERT INTO raw_logs (id, client_ip, http_method, request_uri, raw_payload, decoded_payload, created_at) VALUES
(1, '113.161.72.11', 'POST', '/login.php', 'username=admin%27+OR+1%3D1+--&password=123', "admin' OR 1=1 --", '2026-09-26 01:10:00'),
(2, '185.220.101.5', 'GET', '/search.php?q=%3Cscript%3Ealert(%27XSS%27)%3C/script%3E', '<script>alert(\'XSS\')</script>', '<script>alert(\'XSS\')</script>', '2026-09-26 01:15:30'),
(3, '45.155.205.233', 'POST', '/login.php', 'username=admin%27+UNION+SELECT+1%2C2%2C3--&password=123', "admin' UNION SELECT 1,2,3--", '2026-09-26 01:22:15'),
(4, '103.21.244.5', 'GET', '/product_detail.php?id=1%20OR%201=1', 'id=1 OR 1=1', 'id=1 OR 1=1', '2026-09-26 01:30:00'),
(5, '192.168.1.105', 'GET', '/index.php', 'VIEW_HOMEPAGE', 'VIEW_HOMEPAGE', '2026-09-26 01:35:00');

INSERT INTO ai_labels (log_id, attack_type, confidence_score, risk_score, ai_source, analyzed_at) VALUES
(1, 'SQLi', 0.98, 9, 'Local_ML', '2026-09-26 01:10:01'),
(2, 'XSS', 0.95, 8, 'Local_ML', '2026-09-26 01:15:31'),
(3, 'SQLi', 0.99, 10, 'Cloud_LLM_Fallback', '2026-09-26 01:22:16'),
(4, 'SQLi', 0.91, 8, 'Local_ML', '2026-09-26 01:30:01'),
(5, 'Normal', 0.99, 1, 'Local_ML', '2026-09-26 01:35:01');

-- 3. Nạp mẫu payload cho pentest_payloads
INSERT INTO pentest_payloads (log_id, attack_type, high_risk_payload, pentest_status) VALUES
(1, 'SQLi', "admin' OR 1=1 --", 'PENDING'),
(2, 'XSS', "<script>alert('XSS')</script>", 'TESTED'),
(3, 'SQLi', "admin' UNION SELECT 1,2,3--", 'PENDING');