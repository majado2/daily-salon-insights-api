from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import utc_now
from app.models.entities import AuditLog, RefreshSession, User
from tests.conftest import cashier_headers, login


def issue_csrf(client: TestClient) -> str:
    response = client.get("/api/v1/auth/csrf")
    assert response.status_code == 200
    return response.json()["data"]["csrf_token"]


def login_attempt(client: TestClient, phone: str, pin: str) -> object:
    csrf = issue_csrf(client)
    return client.post(
        "/api/v1/auth/login",
        json={"phone": phone, "pin": pin},
        headers={"X-CSRF-Token": csrf, "Origin": "http://127.0.0.1:4173"},
    )


def test_five_failed_logins_lock_cashier_until_admin_unlocks(
    client: TestClient, seeded: dict[str, object], db_session: Session
) -> None:
    cashier = seeded["cashier"]
    assert isinstance(cashier, User)

    for _ in range(5):
        response = login_attempt(client, cashier.phone, "000000")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"

    db_session.refresh(cashier)
    assert cashier.failed_login_attempts == 5
    assert cashier.locked_until is not None
    assert cashier.locked_until > utc_now()

    locked = login_attempt(client, cashier.phone, "123456")
    assert locked.status_code == 423
    assert locked.json()["error"]["code"] == "ACCOUNT_LOCKED"
    assert locked.json()["error"]["retry_after_seconds"] > 0

    client.cookies.clear()
    admin_csrf = login(client, "0500000001")
    unlocked = client.post(
        f"/api/v1/admin/cashiers/{cashier.id}/unlock",
        headers={"X-CSRF-Token": admin_csrf, "Origin": "http://127.0.0.1:4173"},
    )
    assert unlocked.status_code == 200, unlocked.text

    client.cookies.clear()
    assert login_attempt(client, cashier.phone, "123456").status_code == 200
    event_types = list(
        db_session.scalars(
            select(AuditLog.event_type)
            .where(AuditLog.entity_id == cashier.id)
            .order_by(AuditLog.id)
        )
    )
    assert "auth.account_locked" in event_types
    assert "cashier.unlocked" in event_types


def test_logout_revokes_session_and_clears_authentication(client: TestClient) -> None:
    csrf = login(client, "0500000101")
    assert client.get("/api/v1/auth/me").status_code == 200

    response = client.post(
        "/api/v1/auth/logout",
        headers={"X-CSRF-Token": csrf, "Origin": "http://127.0.0.1:4173"},
    )
    assert response.status_code == 204
    assert client.get("/api/v1/auth/me").status_code == 401


def test_idle_refresh_session_expires(
    client: TestClient, seeded: dict[str, object], db_session: Session
) -> None:
    cashier = seeded["cashier"]
    assert isinstance(cashier, User)
    csrf = login(client, cashier.phone)
    session = db_session.scalar(
        select(RefreshSession)
        .where(RefreshSession.user_id == cashier.id, RefreshSession.revoked_at.is_(None))
        .order_by(RefreshSession.id.desc())
    )
    assert session is not None
    session.last_activity_at = utc_now() - timedelta(minutes=31)
    db_session.commit()

    response = client.post(
        "/api/v1/auth/refresh",
        headers={"X-CSRF-Token": csrf, "Origin": "http://127.0.0.1:4173"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "SESSION_EXPIRED"


def test_pin_change_rejects_old_pin_and_accepts_new_pin(client: TestClient) -> None:
    csrf = login(client, "0500000101")
    changed = client.put(
        "/api/v1/auth/pin",
        json={"current_pin": "123456", "new_pin": "654321"},
        headers={"X-CSRF-Token": csrf, "Origin": "http://127.0.0.1:4173"},
    )
    assert changed.status_code == 200, changed.text
    new_csrf = changed.json()["data"]["csrf_token"]
    assert (
        client.post(
            "/api/v1/auth/logout",
            headers={"X-CSRF-Token": new_csrf, "Origin": "http://127.0.0.1:4173"},
        ).status_code
        == 204
    )

    assert login_attempt(client, "0500000101", "123456").status_code == 401
    assert login_attempt(client, "0500000101", "654321").status_code == 200


def test_csrf_and_role_boundaries_are_enforced(
    client: TestClient, seeded: dict[str, object]
) -> None:
    csrf = login(client, "0500000101")
    employee = seeded["employees"][0]  # type: ignore[index]
    today = seeded["today"]

    forbidden = client.get("/api/v1/admin/branches")
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "FORBIDDEN"

    missing_csrf = client.put(
        f"/api/v1/cashier/today/employees/{employee.id}/sale",  # type: ignore[union-attr]
        json={"service_count": 1, "sales_total": "100.00"},
        headers={"X-Work-Date": today.isoformat(), "Origin": "http://127.0.0.1:4173"},  # type: ignore[union-attr]
    )
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["error"]["code"] == "CSRF_INVALID"

    allowed = client.put(
        f"/api/v1/cashier/today/employees/{employee.id}/sale",  # type: ignore[union-attr]
        json={"service_count": 1, "sales_total": "100.00"},
        headers=cashier_headers(csrf, today),  # type: ignore[arg-type]
    )
    assert allowed.status_code == 200
