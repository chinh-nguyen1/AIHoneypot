-- Version 001: database idempotency for Pipeline Inbox events.
-- Apply once to an existing MySQL 8.0 database after taking and verifying a backup.
-- Historical rows remain NULL; this migration intentionally performs no backfill.
-- Connect with the intended application database selected before executing.

ALTER TABLE raw_logs
    ADD COLUMN ingestion_key_hash BINARY(32) NULL,
    ADD COLUMN ingestion_key_bytes MEDIUMBLOB NULL,
    ADD COLUMN source_record_sha256 BINARY(32) NULL,
    ADD UNIQUE KEY uq_raw_logs_ingestion_key_hash (ingestion_key_hash);
