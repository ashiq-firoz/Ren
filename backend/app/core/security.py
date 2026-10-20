"""Password hashing, JWT access tokens and at-rest encryption for secrets."""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import timedelta
from typing import Any

import bcrypt
import jwt
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings
from app.core.db import utcnow

ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:72], hashed.encode())
    except ValueError:
        return False


def create_access_token(user_id: str, extra: dict[str, Any] | None = None, minutes: int | None = None) -> str:
    now = utcnow()
    payload = {
        "sub": user_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=minutes or settings.access_token_expire_minutes)).timestamp()),
        "typ": "access",
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any] | None:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(("ren-secrets:" + settings.secret_key).encode()).digest())
    return Fernet(key)


def encrypt_secret(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None


def encrypt_json(data: dict[str, Any] | None) -> str | None:
    if not data:
        return None
    return encrypt_secret(json.dumps(data))


def decrypt_json(token: str | None) -> dict[str, Any]:
    raw = decrypt_secret(token)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def mask(value: str | None) -> str | None:
    return "••••" if value else None
