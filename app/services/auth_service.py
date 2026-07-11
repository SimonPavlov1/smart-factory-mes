import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Iterable

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.auth import User


TOKEN_TTL_HOURS = int(os.getenv("AUTH_TOKEN_TTL_HOURS", "12"))
SECRET_KEY = os.getenv("AUTH_SECRET_KEY", "dev-secret-change-me")
security = HTTPBearer(auto_error=False)

ROLE_PERMISSIONS = {
    "admin": ["*"],
    "warehouse": ["inventory:read", "inventory:write", "manufacturing:read", "manufacturing:issue"],
    "manager": ["inventory:read", "production:read", "manufacturing:read", "manufacturing:write", "procurement:write"],
    "engineer": ["inventory:read", "production:read", "production:write"],
    "procurement": ["inventory:read", "procurement:write", "manufacturing:read"],
    "accounting": ["procurement:pay", "manufacturing:read"],
    "assembler": ["production:read", "manufacturing:read", "manufacturing:write"],
    "tester": ["production:read", "manufacturing:read", "quality:write"],
    "repair_engineer": ["production:read", "manufacturing:read", "quality:repair"],
    "packer": ["manufacturing:read", "shipping:pack"],
    "production": ["production:read", "manufacturing:read", "manufacturing:write"],
}


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"pbkdf2_sha256${salt}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, salt, expected = password_hash.split("$", 2)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False

    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000).hex()
    return hmac.compare_digest(digest, expected)


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def create_access_token(user: User) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(hours=TOKEN_TTL_HOURS)
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "exp": int(expires_at.timestamp()),
    }
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(SECRET_KEY.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64encode(signature)}"


def decode_access_token(token: str) -> dict:
    try:
        body, signature = token.split(".", 1)
    except ValueError:
        raise HTTPException(status_code=401, detail="Некорректный токен")

    expected = hmac.new(SECRET_KEY.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64encode(expected), signature):
        raise HTTPException(status_code=401, detail="Некорректная подпись токена")

    payload = json.loads(_b64decode(body))
    if payload.get("exp", 0) < int(datetime.now(timezone.utc).timestamp()):
        raise HTTPException(status_code=401, detail="Сессия истекла")
    return payload


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    if not credentials:
        raise HTTPException(status_code=401, detail="Требуется авторизация")

    payload = decode_access_token(credentials.credentials)
    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Пользователь не найден или отключен")
    return user


def require_roles(*roles: Iterable[str]):
    allowed_roles = set(roles)

    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role == "admin" or user.role in allowed_roles:
            return user
        raise HTTPException(status_code=403, detail="Недостаточно прав")

    return dependency


def permissions_for_role(role: str):
    return ROLE_PERMISSIONS.get(role, [])


def ensure_default_admin(db: Session):
    if db.query(User).first():
        return

    username = os.getenv("DEFAULT_ADMIN_USERNAME", "admin")
    password = os.getenv("DEFAULT_ADMIN_PASSWORD", "admin123")
    db.add(User(
        username=username,
        password_hash=hash_password(password),
        full_name="Администратор",
        role="admin",
        is_active=True,
    ))
    db.commit()
