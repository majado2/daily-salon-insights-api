from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.time import utc_now
from app.models.entities import Branch, BranchDailySettlement, DailySale, Employee, User
from tests.conftest import cashier_headers, login


def admin_headers(csrf: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf, "Origin": "http://127.0.0.1:4173"}


def test_database_rejects_duplicate_employee_day_and_branch_settlement(
    db_session: Session, seeded: dict[str, object]
) -> None:
    branch = seeded["branch"]
    cashier = seeded["cashier"]
    employee = seeded["employees"][0]  # type: ignore[index]
    today = seeded["today"]
    assert isinstance(branch, Branch)
    assert isinstance(cashier, User)
    assert isinstance(employee, Employee)

    first_sale = DailySale(
        branch_id=branch.id,
        employee_id=employee.id,
        work_date=today,  # type: ignore[arg-type]
        service_count=1,
        sales_total=Decimal("100.00"),
        created_by_user_id=cashier.id,
    )
    db_session.add(first_sale)
    db_session.commit()
    db_session.add(
        DailySale(
            branch_id=branch.id,
            employee_id=employee.id,
            work_date=today,  # type: ignore[arg-type]
            service_count=2,
            sales_total=Decimal("200.00"),
            created_by_user_id=cashier.id,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    db_session.add(
        BranchDailySettlement(
            branch_id=branch.id,
            work_date=today,  # type: ignore[arg-type]
            employees_sales_total=Decimal("100.00"),
            row_created_at=utc_now(),
        )
    )
    db_session.commit()
    db_session.add(
        BranchDailySettlement(
            branch_id=branch.id,
            work_date=today,  # type: ignore[arg-type]
            employees_sales_total=Decimal("100.00"),
            row_created_at=utc_now(),
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_close_and_reopen_day_changes_completion_without_creating_activity(
    client: TestClient, seeded: dict[str, object]
) -> None:
    branch = seeded["branch"]
    today = seeded["today"]
    assert isinstance(branch, Branch)
    work_date = today + timedelta(days=2)  # type: ignore[operator]
    csrf = login(client, "0500000001")

    closed = client.post(
        "/api/v1/admin/branch-closures",
        json={"branch_id": branch.id, "work_date": work_date.isoformat(), "reason": "صيانة"},
        headers=admin_headers(csrf),
    )
    assert closed.status_code == 201, closed.text
    closure_id = closed.json()["data"]["id"]
    detail = client.get(f"/api/v1/admin/branches/{branch.id}/days/{work_date}").json()["data"]
    assert detail["completion"]["status"] == "closed"
    assert detail["employees"] == []
    assert detail["settlement"] is None

    reopened = client.post(
        f"/api/v1/admin/branch-closures/{closure_id}/reopen",
        json={"reason": "انتهاء الصيانة"},
        headers=admin_headers(csrf),
    )
    assert reopened.status_code == 200, reopened.text
    reopened_detail = client.get(f"/api/v1/admin/branches/{branch.id}/days/{work_date}").json()[
        "data"
    ]
    assert reopened_detail["completion"]["status"] == "incomplete"
    assert len(reopened_detail["employees"]) == 2


def test_day_with_any_sale_cannot_be_closed(client: TestClient, seeded: dict[str, object]) -> None:
    branch = seeded["branch"]
    employee = seeded["employees"][0]  # type: ignore[index]
    today = seeded["today"]
    assert isinstance(branch, Branch)
    assert isinstance(employee, Employee)
    cashier_csrf = login(client, "0500000101")
    saved = client.put(
        f"/api/v1/cashier/today/employees/{employee.id}/sale",
        json={"service_count": 0, "sales_total": "0.00"},
        headers=cashier_headers(cashier_csrf, today),  # type: ignore[arg-type]
    )
    assert saved.status_code == 200

    client.cookies.clear()
    admin_csrf = login(client, "0500000001")
    response = client.post(
        "/api/v1/admin/branch-closures",
        json={"branch_id": branch.id, "work_date": today.isoformat(), "reason": "محاولة إغلاق"},  # type: ignore[union-attr]
        headers=admin_headers(admin_csrf),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DAY_HAS_ACTIVITY"


def test_disabling_branch_blocks_cashier_login_on_effective_date(
    client: TestClient, seeded: dict[str, object]
) -> None:
    branch = seeded["branch"]
    today = seeded["today"]
    assert isinstance(branch, Branch)
    csrf = login(client, "0500000001")
    disabled = client.post(
        f"/api/v1/admin/branches/{branch.id}/status-changes",
        json={"status": "disabled", "effective_date": today.isoformat(), "note": "إيقاف مؤقت"},  # type: ignore[union-attr]
        headers=admin_headers(csrf),
    )
    assert disabled.status_code == 200, disabled.text

    client.cookies.clear()
    cashier_login = client.get("/api/v1/auth/csrf")
    login_csrf = cashier_login.json()["data"]["csrf_token"]
    response = client.post(
        "/api/v1/auth/login",
        json={"phone": "0500000101", "pin": "123456"},
        headers={"X-CSRF-Token": login_csrf, "Origin": "http://127.0.0.1:4173"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "BRANCH_DISABLED"
