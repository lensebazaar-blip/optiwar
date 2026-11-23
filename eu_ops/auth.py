"""Authentication: password hashing, JWT tokens, OTP generation."""
import hashlib
import os
import random
import string
from datetime import datetime, timedelta, timezone

from jose import jwt, JWTError

import config
from database import db_cursor


def hash_password(password: str) -> str:
    salt = os.urandom(32)
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
    return salt.hex() + ':' + key.hex()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        salt_hex, key_hex = hashed.split(':', 1)
        salt = bytes.fromhex(salt_hex)
        expected_key = bytes.fromhex(key_hex)
        test_key = hashlib.pbkdf2_hmac('sha256', plain.encode('utf-8'), salt, 100000)
        return test_key == expected_key
    except Exception:
        return False


def create_token(user_id: int, username: str, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=config.JWT_EXPIRE_MINUTES)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "exp": expire,
    }
    return jwt.encode(payload, config.SECRET_KEY, algorithm=config.JWT_ALGORITHM)


def decode_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, config.SECRET_KEY, algorithms=[config.JWT_ALGORITHM])
        return payload
    except JWTError:
        return None


def generate_otp() -> str:
    return "".join(random.choices(string.digits, k=config.OTP_LENGTH))


def store_otp(user_id: int, code: str):
    expires = datetime.now() + timedelta(minutes=config.OTP_EXPIRE_MINUTES)
    with db_cursor() as cur:
        # Invalidate old codes
        cur.execute("UPDATE otp_codes SET used = 1 WHERE user_id = %s AND used = 0", (user_id,))
        cur.execute(
            "INSERT INTO otp_codes (user_id, code, expires_at) VALUES (%s, %s, %s)",
            (user_id, code, expires),
        )


def verify_otp(user_id: int, code: str) -> bool:
    with db_cursor() as cur:
        cur.execute(
            """SELECT id FROM otp_codes
               WHERE user_id = %s AND code = %s AND used = 0 AND expires_at > NOW()
               ORDER BY id DESC LIMIT 1""",
            (user_id, code),
        )
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE otp_codes SET used = 1 WHERE id = %s", (row["id"],))
            return True
    return False


def get_user_by_username(username: str) -> dict | None:
    with db_cursor() as cur:
        cur.execute(
            "SELECT * FROM platform_users WHERE username = %s AND is_active = 1",
            (username,),
        )
        return cur.fetchone()


def get_user_by_id(user_id: int) -> dict | None:
    with db_cursor() as cur:
        cur.execute(
            "SELECT * FROM platform_users WHERE id = %s AND is_active = 1",
            (user_id,),
        )
        return cur.fetchone()


def create_user(username: str, password: str, email: str, role: str, full_name: str) -> int:
    hashed = hash_password(password)
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO platform_users (username, password_hash, email, role, full_name)
               VALUES (%s, %s, %s, %s, %s)""",
            (username, hashed, email, role, full_name),
        )
        return cur.lastrowid


def log_activity(user_id: int | None, action: str, entity_type: str = None,
                 entity_id: int = None, details: dict = None, ip: str = None):
    import json
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO activity_log (user_id, action, entity_type, entity_id, details, ip_address)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (user_id, action, entity_type, entity_id,
             json.dumps(details) if details else None, ip),
        )
