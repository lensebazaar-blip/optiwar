"""Schema of the Ops import console: the staging rows and the append-only
log. In its own module so ``contact_lens.TABLES`` (which the deploy tool reads
to plan the migration) can include it without importing the console, which
imports the catalogue, which imports ``contact_lens``.
"""

STAGING_SCHEMA = """
CREATE TABLE IF NOT EXISTS contact_lens_import_staging (
    staging_id     BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    source_system  VARCHAR(40) NOT NULL,
    source_ref     VARCHAR(80) NOT NULL,
    status         VARCHAR(12) NOT NULL,
    payload_json   LONGTEXT NOT NULL,
    payload_sha256 CHAR(64) NOT NULL,
    report_json    LONGTEXT NOT NULL,
    review_json    LONGTEXT NULL,
    product_id     INT NULL,
    created_by     VARCHAR(80) NULL,
    created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_at    DATETIME NULL,
    confirmed_by   VARCHAR(80) NULL,
    confirmed_at   DATETIME NULL,
    KEY ix_cl_staging_ref (source_system, source_ref, status),
    KEY ix_cl_staging_sha (payload_sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# Append-only: nothing in this module updates or deletes a row of it.
LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS contact_lens_import_log (
    log_id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    action         VARCHAR(12) NOT NULL,
    staging_id     BIGINT UNSIGNED NULL,
    product_id     INT NULL,
    source_ref     VARCHAR(80) NULL,
    actor          VARCHAR(80) NULL,
    outcome        VARCHAR(12) NOT NULL,
    detail_json    TEXT NULL,
    payload_sha256 CHAR(64) NULL,
    occurred_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY ix_cl_log_product (product_id, log_id),
    KEY ix_cl_log_staging (staging_id, log_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

TABLES = (
    ("contact_lens_import_staging", STAGING_SCHEMA),
    ("contact_lens_import_log", LOG_SCHEMA),
)
