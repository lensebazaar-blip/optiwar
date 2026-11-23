"""Database connection pool using PyMySQL."""
import pymysql
from pymysql.cursors import DictCursor
from contextlib import contextmanager
import config


def get_connection(database=None):
    return pymysql.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        user=config.DB_USER,
        password=config.DB_PASS,
        database=database or config.DB_NAME,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=True,
    )


@contextmanager
def db_cursor(database=None):
    conn = get_connection(database)
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


def get_ow_connection():
    """Connect to the remote optiwar2 database on 172.105.54.11."""
    return pymysql.connect(
        host=config.OW_DB_HOST,
        port=config.OW_DB_PORT,
        user=config.OW_DB_USER,
        password=config.OW_DB_PASS,
        database=config.OW_DB_NAME,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=True,
        connect_timeout=10,
        read_timeout=30,
    )


@contextmanager
def ow_cursor():
    """Cursor for the optiwar2 database (remote server)."""
    conn = get_ow_connection()
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


def ow_write_enabled() -> bool:
    return bool(config.OW_WRITE_DB_USER and config.OW_WRITE_DB_PASS)


@contextmanager
def ow_ops_cursor():
    """Autocommit cursor on optiwar2 under the ops_writer account, for the
    ops-owned `ops_*` tables (AWBs, notification logs, RX change log, drafts).
    Reads of those tables may use ow_cursor(); writes must come through here."""
    if not ow_write_enabled():
        raise RuntimeError("OW_WRITE_DB_USER/OW_WRITE_DB_PASS are not configured")
    conn = pymysql.connect(
        host=config.OW_DB_HOST,
        port=config.OW_DB_PORT,
        user=config.OW_WRITE_DB_USER,
        password=config.OW_WRITE_DB_PASS,
        database=config.OW_DB_NAME,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=True,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION innodb_lock_wait_timeout = 2")
            yield cur
    finally:
        conn.close()


@contextmanager
def ow_write_transaction():
    """One short transaction on optiwar2 under the INSERT-only account.

    The storefront serves live checkouts on this database, so the connection is
    deliberately impatient: READ COMMITTED, and a 2 second lock wait so ops gives
    up rather than making a customer queue behind it. Inserting into `orders`
    checks its foreign key to `products`, which does wait behind a checkout
    deducting that product's stock — hence the short timeout. Callers retry on
    deadlock: a rolled-back transaction is safe to repeat."""
    if not ow_write_enabled():
        raise RuntimeError("OW_WRITE_DB_USER/OW_WRITE_DB_PASS are not configured")
    conn = pymysql.connect(
        host=config.OW_DB_HOST,
        port=config.OW_DB_PORT,
        user=config.OW_WRITE_DB_USER,
        password=config.OW_WRITE_DB_PASS,
        database=config.OW_DB_NAME,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=False,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION innodb_lock_wait_timeout = 2")
            cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
            cur.execute("START TRANSACTION")
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def db_transaction():
    conn = get_connection()
    conn.autocommit(False)
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_platform_tables():
    """Create platform-specific tables if they don't exist."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS platform_users (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    username VARCHAR(50) UNIQUE NOT NULL,
                    password_hash VARCHAR(255) NOT NULL,
                    email VARCHAR(100) NOT NULL,
                    role ENUM('admin', 'processing') NOT NULL DEFAULT 'processing',
                    full_name VARCHAR(100) NOT NULL DEFAULT '',
                    is_active TINYINT(1) NOT NULL DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS otp_codes (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT NOT NULL,
                    code VARCHAR(10) NOT NULL,
                    expires_at DATETIME NOT NULL,
                    used TINYINT(1) NOT NULL DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_user_code (user_id, code),
                    FOREIGN KEY (user_id) REFERENCES platform_users(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS approval_requests (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    order_id INT NOT NULL,
                    requested_by INT NOT NULL,
                    action_type ENUM('edit_order', 'refund_full', 'refund_partial', 'partial_ship', 'cancel_order', 'other') NOT NULL,
                    action_data JSON,
                    status ENUM('pending', 'approved', 'rejected') NOT NULL DEFAULT 'pending',
                    admin_notes TEXT,
                    reviewed_by INT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    reviewed_at DATETIME,
                    INDEX idx_status (status),
                    INDEX idx_order (order_id),
                    FOREIGN KEY (requested_by) REFERENCES platform_users(id),
                    FOREIGN KEY (reviewed_by) REFERENCES platform_users(id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS shipping_awb (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    order_id INT NOT NULL,
                    tracking_number VARCHAR(100),
                    courier VARCHAR(50) NOT NULL DEFAULT 'FedEx',
                    awb_status ENUM('created', 'shipped', 'in_transit', 'delivered', 'cancelled', 'returned') NOT NULL DEFAULT 'created',
                    label_pdf_path VARCHAR(255),
                    fedex_response JSON,
                    created_by INT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_order (order_id),
                    INDEX idx_tracking (tracking_number),
                    FOREIGN KEY (created_by) REFERENCES platform_users(id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS activity_log (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT,
                    action VARCHAR(100) NOT NULL,
                    entity_type VARCHAR(50),
                    entity_id INT,
                    details JSON,
                    ip_address VARCHAR(45),
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_user (user_id),
                    INDEX idx_entity (entity_type, entity_id),
                    INDEX idx_created (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

        conn.commit()
    finally:
        conn.close()


def get_inv_connection():
    """Connect to the inventory database on 192.168.0.176."""
    return pymysql.connect(
        host=config.INV_DB_HOST,
        port=config.INV_DB_PORT,
        user=config.INV_DB_USER,
        password=config.INV_DB_PASS,
        database=config.INV_DB_NAME,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=True,
        connect_timeout=10,
        read_timeout=30,
    )


@contextmanager
def inv_cursor():
    """Cursor for the inventory database (192.168.0.176)."""
    conn = get_inv_connection()
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()
