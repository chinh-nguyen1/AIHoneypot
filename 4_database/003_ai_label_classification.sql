-- Version 003: classification outcome columns for ai_labels.
-- Apply once to an existing MySQL 8.0 database after taking and verifying a backup.
-- NEEDS_REVIEW events keep NULL attack_type, risk_score and (when the model did not run) confidence_score.
-- Connect with the intended application database selected before executing.

ALTER TABLE ai_labels
    ADD COLUMN classification_status VARCHAR(32) NOT NULL AFTER log_id,
    ADD COLUMN classification_reason VARCHAR(64) NULL AFTER classification_status,
    MODIFY COLUMN attack_type VARCHAR(50) NULL,
    MODIFY COLUMN confidence_score FLOAT NULL,
    MODIFY COLUMN risk_score INT NULL;
