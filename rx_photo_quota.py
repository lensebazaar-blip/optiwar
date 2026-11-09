"""Server-side limit on the prescription photo reader (rx_photo.py).

Each read is a paid vision-model call and the reader needs no sign-in, so a
count kept only in the session cookie can be reset by dropping the cookie.
Reads are counted here per client IP per day, and in one overall row per day
that holds even if a caller changes or forges its IP.

Stdlib only, so the deploy tool can read ``TABLES`` plainly.
"""
import datetime

MAX_READS_PER_MINUTE = 4           # a person retrying is fine; a script is not
BLOCK_HOURS = 24
ALL_CLIENTS = "*"
KEEP_DAYS = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS rx_photo_quota (
    client_ip VARCHAR(64) NOT NULL,
    day DATE NOT NULL,
    read_count INT UNSIGNED NOT NULL DEFAULT 0,
    PRIMARY KEY (client_ip, day),
    KEY ix_rx_quota_day (day)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""
FLAGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS rx_photo_flags (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    created_at DATETIME NOT NULL,
    category VARCHAR(24) NOT NULL,
    sha256 CHAR(64) NOT NULL,
    client_ip VARCHAR(64) NOT NULL,
    customer_id INT NULL,
    reviewed_at DATETIME NULL,
    reviewed_by VARCHAR(191) NULL,
    KEY ix_rx_flags_ip (client_ip, created_at),
    KEY ix_rx_flags_open (reviewed_at, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""
TABLES = (("rx_photo_quota", SCHEMA), ("rx_photo_flags", FLAGS_SCHEMA))

_SCHEMA_READY = False


def ensure_schema(cursor):
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    for _name, ddl in TABLES:
        cursor.execute(ddl)
    _SCHEMA_READY = True


def _count_one(cursor, client, day):
    """Add one read for this key and return the new count. The row stays locked
    until the caller commits, so two simultaneous reads cannot both slip in."""
    cursor.execute(
        "INSERT INTO rx_photo_quota (client_ip, day, read_count) VALUES (%s, %s, 1) "
        "ON DUPLICATE KEY UPDATE read_count = read_count + 1", (client, day))
    cursor.execute("SELECT read_count FROM rx_photo_quota WHERE client_ip = %s AND day = %s", (client, day))
    return int(cursor.fetchone()["read_count"])


def reserve(cursor, client_ip, per_ip, per_day, now=None):
    """Count one read; True when it is within every limit. The caller commits
    before calling the model, so no lock is held during that call."""
    now = now or datetime.datetime.now()
    day = now.date()
    client = (client_ip or "unknown")[:45]
    if _count_one(cursor, "min:%s:%s" % (now.strftime("%H%M"), client), day) > MAX_READS_PER_MINUTE:
        return False
    if _count_one(cursor, client, day) > per_ip:
        return False
    total = _count_one(cursor, ALL_CLIENTS, day)
    if total == 1:      # first read of the day: old rows are no longer needed
        cursor.execute("DELETE FROM rx_photo_quota WHERE day < %s OR (day < %s AND client_ip LIKE 'min:%%')",
                       (day - datetime.timedelta(days=KEEP_DAYS), day))
    return total <= per_day


def is_blocked(cursor, client_ip, now=None):
    """True while this IP has an unreviewed flagged upload from the last day."""
    now = now or datetime.datetime.now()
    cursor.execute("SELECT COUNT(*) AS n FROM rx_photo_flags WHERE client_ip = %s AND reviewed_at IS NULL "
                   "AND created_at > %s", ((client_ip or "unknown")[:45], now - datetime.timedelta(hours=BLOCK_HOURS)))
    row = cursor.fetchone()
    return bool(row and row.get("n"))


def flag(cursor, category, sha256, client_ip, customer_id=None, now=None):
    """Record a refused upload. Only facts are kept: never the image."""
    cursor.execute("INSERT INTO rx_photo_flags (created_at, category, sha256, client_ip, customer_id) "
                   "VALUES (%s, %s, %s, %s, %s)",
                   (now or datetime.datetime.now(), category, sha256, (client_ip or "unknown")[:45], customer_id))
