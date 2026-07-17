import hashlib
import hmac
import secrets
from datetime import timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Request, Response

from app.core.config import Settings
from app.core.errors import BusinessError
from app.core.time import utc_now

password_hasher = PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1)


def hash_pin(pin: str) -> str:
    return password_hasher.hash(pin)


def verify_pin(pin_hash: str, pin: str) -> bool:
    try:
        return password_hasher.verify(pin_hash, pin)
    except VerifyMismatchError:
        return False


def random_token(length: int = 32) -> str:
    return secrets.token_urlsafe(length)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def make_access_token(user_id: int, session_id: int, settings: Settings) -> str:
    now = utc_now()
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "sid": session_id,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_access_token(token: str, settings: Settings) -> dict[str, Any]:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise BusinessError(401, "SESSION_EXPIRED", "The session has expired.") from exc


def verify_csrf(request: Request, settings: Settings) -> None:
    cookie_value = request.cookies.get("salon_csrf")
    header_value = request.headers.get("X-CSRF-Token")
    if not cookie_value or not header_value or not hmac.compare_digest(cookie_value, header_value):
        raise BusinessError(403, "CSRF_INVALID", "The security token is invalid.")
    origin = request.headers.get("Origin")
    if origin and origin not in settings.allowed_origins:
        raise BusinessError(403, "ORIGIN_NOT_ALLOWED", "The request origin is not allowed.")


def set_csrf_cookie(response: Response, csrf_token: str, settings: Settings) -> None:
    response.set_cookie(
        "salon_csrf",
        csrf_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path=settings.api_prefix,
        max_age=settings.session_absolute_hours * 3600,
    )


def set_session_cookies(
    response: Response,
    access_token: str,
    refresh_token: str,
    csrf_token: str,
    settings: Settings,
) -> None:
    response.set_cookie(
        "salon_access",
        access_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path=settings.api_prefix,
        max_age=settings.access_token_minutes * 60,
    )
    response.set_cookie(
        "salon_refresh",
        refresh_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path=f"{settings.api_prefix}/auth",
        max_age=settings.session_absolute_hours * 3600,
    )
    set_csrf_cookie(response, csrf_token, settings)


def clear_session_cookies(response: Response, settings: Settings) -> None:
    response.delete_cookie("salon_access", path=settings.api_prefix)
    response.delete_cookie("salon_refresh", path=f"{settings.api_prefix}/auth")
    response.delete_cookie("salon_csrf", path=settings.api_prefix)
