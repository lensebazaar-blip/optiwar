"""Server-side limit on the prescription photo reader (rx_photo.py).

Each read is a paid vision-model call and the reader needs no sign-in, so a
count kept only in the session cookie can be reset by dropping the cookie.
Reads are counted here per client IP per day, and in one overall row per day
that holds even if a caller changes or forges its IP.

Stdlib only, so the deploy tool can read ``TABLES`` plainly.
"""
import datetime

MAX_READS_PER_IP_PER_DAY = 20
MAX_READS_PER_DAY = 300
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
TABLES = (("rx_photo_quota", SCHEMA),)

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


def reserve(cursor, client_ip, day=None):
    """Count one read; True when it is within both limits. The caller commits
    before calling the model, so no lock is held during that call."""
    day = day or datetime.date.today()
    client = (client_ip or "unknown")[:64]
    if _count_one(cursor, client, day) > MAX_READS_PER_IP_PER_DAY:
        return False
    total = _count_one(cursor, ALL_CLIENTS, day)
    if total == 1:      # first read of the day: old rows are no longer needed
        cursor.execute("DELETE FROM rx_photo_quota WHERE day < %s", (day - datetime.timedelta(days=KEEP_DAYS),))
    return total <= MAX_READS_PER_DAY
