-- Version 002: persist optional AI recommendations with their classification.
-- Apply once to an existing MySQL 8.0 database after taking and verifying a backup.
-- Existing labels remain NULL; this migration intentionally performs no backfill.
-- Connect with the intended application database selected before executing.

ALTER TABLE ai_labels
    ADD COLUMN recommendation TEXT NULL;
