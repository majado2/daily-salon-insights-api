from datetime import timedelta
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import BusinessError
from app.core.security import decode_access_token, verify_csrf
from app.core.time import riyadh_today, utc_now
from app.db.session import get_db
from app.models.entities import EntityStatus, RefreshSession, User, UserRole
from app.services.business import branch_status_at

DbSession = Annotated[Session, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def envelope(request: Request, data: Any, *, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata = {"request_id": request.state.request_id}
    if meta:
        metadata.update(meta)
    return {"data": data, "meta": metadata}


def require_csrf(request: Request, settings: AppSettings) -> None:
    verify_csrf(request, settings)


def get_current_user(request: Request, db: DbSession, settings: AppSettings) -> User:
    token = request.cookies.get("salon_access")
    if not token:
        raise BusinessError(401, "AUTHENTICATION_REQUIRED", "Authentication is required.")
    payload = decode_access_token(token, settings)
    try:
        user_id = int(payload["sub"])
        session_id = int(payload["sid"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BusinessError(401, "SESSION_EXPIRED", "The session has expired.") from exc
    session = db.get(RefreshSession, session_id)
    now = utc_now()
    if (
        session is None
        or session.user_id != user_id
        or session.revoked_at is not None
        or session.absolute_expires_at <= now
        or session.last_activity_at + timedelta(minutes=settings.session_idle_minutes) <= now
    ):
        raise BusinessError(401, "SESSION_EXPIRED", "The session has expired.")
    user = db.get(User, user_id)
    if user is None:
        raise BusinessError(401, "SESSION_EXPIRED", "The session has expired.")
    if user.status != EntityStatus.ACTIVE:
        raise BusinessError(403, "ACCOUNT_DISABLED", "The account is disabled.")
    if user.role == UserRole.CASHIER and (
        user.branch_id is None
        or branch_status_at(db, user.branch_id, riyadh_today()) != EntityStatus.ACTIVE
    ):
        raise BusinessError(403, "BRANCH_DISABLED", "The assigned branch is disabled.")
    if session.last_activity_at + timedelta(minutes=5) <= now:
        session.last_activity_at = now
        db.commit()
    request.state.session_id = session_id
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUser) -> User:
    if user.role != UserRole.ADMIN:
        raise BusinessError(403, "FORBIDDEN", "Administrator access is required.")
    return user


def require_primary_admin(user: Annotated[User, Depends(require_admin)]) -> User:
    if not user.is_primary_admin:
        raise BusinessError(403, "FORBIDDEN", "Primary administrator access is required.")
    return user


def require_cashier(user: CurrentUser) -> User:
    if user.role != UserRole.CASHIER:
        raise BusinessError(403, "FORBIDDEN", "Cashier access is required.")
    return user


AdminUser = Annotated[User, Depends(require_admin)]
PrimaryAdminUser = Annotated[User, Depends(require_primary_admin)]
CashierUser = Annotated[User, Depends(require_cashier)]
