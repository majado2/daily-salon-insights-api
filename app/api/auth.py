from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select, update

from app.api.deps import (
    AppSettings,
    CurrentUser,
    DbSession,
    envelope,
    require_csrf,
)
from app.core.errors import BusinessError
from app.core.security import (
    clear_session_cookies,
    hash_pin,
    make_access_token,
    random_token,
    set_csrf_cookie,
    set_session_cookies,
    token_hash,
    verify_pin,
)
from app.core.time import iso_utc, riyadh_today, utc_now
from app.models.entities import AuditLog, EntityStatus, RefreshSession, User, UserRole
from app.schemas.requests import LoginRequest, PinChangeRequest, PreferenceRequest
from app.services.business import add_audit, branch_status_at, branch_summary

router = APIRouter(prefix="/auth", tags=["Authentication"])


def auth_user_view(db: DbSession, user: User) -> dict[str, object]:
    return {
        "id": user.id,
        "display_name": user.display_name,
        "role": user.role.value,
        "is_primary_admin": user.is_primary_admin,
        "preferred_language": user.preferred_language.value,
        "branch": branch_summary(db, user.branch) if user.branch else None,
    }


@router.get("/csrf")
def csrf(request: Request, response: Response, settings: AppSettings) -> dict[str, object]:
    value = random_token()
    set_csrf_cookie(response, value, settings)
    response.headers["Cache-Control"] = "no-store"
    return envelope(request, {"csrf_token": value})


@router.post("/login", dependencies=[Depends(require_csrf)])
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: DbSession,
    settings: AppSettings,
) -> dict[str, object]:
    user = db.scalar(select(User).where(User.phone == payload.phone))
    now = utc_now()
    if user and user.locked_until and user.locked_until > now:
        retry = max(1, int((user.locked_until - now).total_seconds()))
        raise BusinessError(
            423,
            "ACCOUNT_LOCKED",
            "The account is temporarily locked.",
            extra={"retry_after_seconds": retry},
        )
    if user is None or not verify_pin(user.pin_hash, payload.pin):
        if user:
            user.failed_login_attempts += 1
            if user.failed_login_attempts >= 5:
                user.locked_until = now + timedelta(minutes=15)
                add_audit(
                    db,
                    actor=user,
                    event_type="auth.account_locked",
                    entity_type="user",
                    entity_id=user.id,
                    branch_id=user.branch_id,
                )
            else:
                db.add(
                    AuditLog(
                        actor_user_id=user.id,
                        event_type="auth.login_failed",
                        entity_type="user",
                        entity_id=user.id,
                        branch_id=user.branch_id,
                        occurred_at=now,
                    )
                )
            db.commit()
        raise BusinessError(401, "INVALID_CREDENTIALS", "Invalid phone number or PIN.")
    if user.status != EntityStatus.ACTIVE:
        raise BusinessError(403, "ACCOUNT_DISABLED", "The account is disabled.")
    if user.role == UserRole.CASHIER and (
        user.branch_id is None
        or branch_status_at(db, user.branch_id, riyadh_today()) != EntityStatus.ACTIVE
    ):
        raise BusinessError(403, "BRANCH_DISABLED", "The assigned branch is disabled.")

    user.failed_login_attempts = 0
    user.locked_until = None
    user.last_login_at = now
    refresh_value = random_token(48)
    session = RefreshSession(
        user_id=user.id,
        token_hash=token_hash(refresh_value),
        created_at=now,
        last_activity_at=now,
        absolute_expires_at=now + timedelta(hours=settings.session_absolute_hours),
    )
    db.add(session)
    db.flush()
    csrf_value = random_token()
    access_value = make_access_token(user.id, session.id, settings)
    db.commit()
    set_session_cookies(response, access_value, refresh_value, csrf_value, settings)
    response.headers["Cache-Control"] = "no-store"
    return envelope(
        request,
        {
            "user": auth_user_view(db, user),
            "session": {
                "absolute_expires_at": iso_utc(session.absolute_expires_at),
                "idle_timeout_seconds": settings.session_idle_minutes * 60,
            },
            "csrf_token": csrf_value,
        },
    )


@router.post("/refresh", dependencies=[Depends(require_csrf)])
def refresh(
    request: Request,
    response: Response,
    db: DbSession,
    settings: AppSettings,
) -> dict[str, object]:
    raw = request.cookies.get("salon_refresh")
    if not raw:
        raise BusinessError(401, "SESSION_EXPIRED", "The session has expired.")
    session = db.scalar(select(RefreshSession).where(RefreshSession.token_hash == token_hash(raw)))
    now = utc_now()
    if (
        session is None
        or session.revoked_at is not None
        or session.absolute_expires_at <= now
        or session.last_activity_at + timedelta(minutes=settings.session_idle_minutes) <= now
    ):
        raise BusinessError(401, "SESSION_EXPIRED", "The session has expired.")
    user = db.get(User, session.user_id)
    if user is None or user.status != EntityStatus.ACTIVE:
        raise BusinessError(401, "SESSION_EXPIRED", "The session has expired.")
    new_refresh = random_token(48)
    session.token_hash = token_hash(new_refresh)
    session.last_activity_at = now
    csrf_value = random_token()
    access_value = make_access_token(user.id, session.id, settings)
    db.commit()
    set_session_cookies(response, access_value, new_refresh, csrf_value, settings)
    response.headers["Cache-Control"] = "no-store"
    return envelope(
        request,
        {
            "session": {
                "absolute_expires_at": iso_utc(session.absolute_expires_at),
                "idle_timeout_seconds": settings.session_idle_minutes * 60,
            },
            "csrf_token": csrf_value,
        },
    )


@router.post("/logout", status_code=204, dependencies=[Depends(require_csrf)])
def logout(
    request: Request,
    response: Response,
    user: CurrentUser,
    db: DbSession,
    settings: AppSettings,
) -> None:
    del user
    session = db.get(RefreshSession, request.state.session_id)
    if session and session.revoked_at is None:
        session.revoked_at = utc_now()
        db.commit()
    clear_session_cookies(response, settings)
    response.status_code = 204


@router.get("/me")
def me(
    request: Request,
    user: CurrentUser,
    db: DbSession,
    settings: AppSettings,
) -> dict[str, object]:
    session = db.get(RefreshSession, request.state.session_id)
    return envelope(
        request,
        {
            "user": auth_user_view(db, user),
            "session": {
                "absolute_expires_at": iso_utc(session.absolute_expires_at) if session else None,
                "idle_timeout_seconds": settings.session_idle_minutes * 60,
            },
        },
    )


@router.patch("/preferences", dependencies=[Depends(require_csrf)])
def preferences(
    payload: PreferenceRequest,
    request: Request,
    user: CurrentUser,
    db: DbSession,
) -> dict[str, object]:
    user.preferred_language = payload.preferred_language  # type: ignore[assignment]
    db.commit()
    return envelope(request, auth_user_view(db, user))


@router.put("/pin", dependencies=[Depends(require_csrf)])
def change_pin(
    payload: PinChangeRequest,
    request: Request,
    response: Response,
    user: CurrentUser,
    db: DbSession,
    settings: AppSettings,
) -> dict[str, object]:
    if not verify_pin(user.pin_hash, payload.current_pin):
        raise BusinessError(401, "INVALID_CURRENT_PIN", "The current PIN is invalid.")
    user.pin_hash = hash_pin(payload.new_pin)
    now = utc_now()
    db.execute(
        update(RefreshSession)
        .where(RefreshSession.user_id == user.id, RefreshSession.id != request.state.session_id)
        .values(revoked_at=now)
    )
    add_audit(
        db,
        actor=user,
        event_type="auth.pin_changed",
        entity_type="user",
        entity_id=user.id,
        branch_id=user.branch_id,
    )
    session = db.get(RefreshSession, request.state.session_id)
    if session is None:
        raise BusinessError(401, "SESSION_EXPIRED", "The session has expired.")
    refresh_value = random_token(48)
    session.token_hash = token_hash(refresh_value)
    session.last_activity_at = now
    csrf_value = random_token()
    access_value = make_access_token(user.id, session.id, settings)
    db.commit()
    set_session_cookies(response, access_value, refresh_value, csrf_value, settings)
    return envelope(request, {"csrf_token": csrf_value})
