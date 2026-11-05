CREATE DATABASE IF NOT EXISTS ai_webhoneypot_db
CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE ai_webhoneypot_db;

-- Bảng 1: Lưu log thô từ Web Bẫy
CREATE TABLE IF NOT EXISTS raw_logs (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    client_ip VARCHAR(45) NOT NULL,
    http_method VARCHAR(10) NOT NULL,
    request_uri TEXT NOT NULL,
    raw_payload TEXT,
    decoded_payload TEXT,
    ingestion_key_hash BINARY(32) NULL,
    ingestion_key_bytes MEDIUMBLOB NULL,
    source_record_sha256 BINARY(32) NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_raw_logs_ingestion_key_hash (ingestion_key_hash),
    INDEX idx_client_ip (client_ip)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Bảng 2: Lưu nhãn dự đoán của AI
CREATE TABLE IF NOT EXISTS ai_labels (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    log_id BIGINT NOT NULL,
    attack_type VARCHAR(50) NOT NULL,
    confidence_score FLOAT NOT NULL,
    risk_score INT NOT NULL,
    ai_source VARCHAR(50) DEFAULT 'Local_ML',
    recommendation TEXT NULL,
    analyzed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (log_id) REFERENCES raw_logs(id) ON DELETE CASCADE,
    INDEX idx_attack_type (attack_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Bảng 3: Lưu tọa độ địa lý IP cho Bản đồ SOC
CREATE TABLE IF NOT EXISTS threat_ips (
    ip_address VARCHAR(45) PRIMARY KEY,
    country_name VARCHAR(100) DEFAULT 'Unknown',
    latitude DECIMAL(10, 8) DEFAULT 0.0,
    longitude DECIMAL(11, 8) DEFAULT 0.0,
    total_attacks INT DEFAULT 1,
    last_attack_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Bảng 4: Lưu mẫu Payload nguy hiểm
CREATE TABLE IF NOT EXISTS pentest_payloads (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    log_id BIGINT NOT NULL,
    attack_type VARCHAR(50) NOT NULL,
    high_risk_payload TEXT NOT NULL,
    pentest_status VARCHAR(20) DEFAULT 'PENDING',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (log_id) REFERENCES raw_logs(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;