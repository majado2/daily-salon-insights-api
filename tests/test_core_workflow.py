from datetime import timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import (
    Branch,
    BranchDailySettlement,
    BranchStatusPeriod,
    DailySale,
    Employee,
    EmployeeBranchAssignment,
    EntityStatus,
    User,
)
from app.services.business import money
from tests.conftest import cashier_headers, login


def test_cashier_work_date_header_is_allowed_by_cors(client: TestClient) -> None:
    response = client.options(
        "/api/v1/cashier/today/settlement",
        headers={
            "Origin": "http://127.0.0.1:4173",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "x-csrf-token,x-work-date",
        },
    )
    assert response.status_code == 200
    assert "x-work-date" in response.headers["access-control-allow-headers"].lower()


def test_cashier_stale_screen_cannot_write_after_work_date_changes(
    client: TestClient, seeded: dict[str, object]
) -> None:
    employee = seeded["employees"][0]  # type: ignore[index]
    today = seeded["today"]
    assert isinstance(employee, Employee)
    csrf = login(client, "0500000101")
    stale_headers = cashier_headers(csrf, today - timedelta(days=1))  # type: ignore[operator]

    sale_response = client.put(
        f"/api/v1/cashier/today/employees/{employee.id}/sale",
        json={"service_count": 1, "sales_total": "100.00"},
        headers=stale_headers,
    )
    assert sale_response.status_code == 409
    assert sale_response.json()["error"]["code"] == "CASHIER_EDIT_WINDOW_CLOSED"

    settlement_response = client.put(
        "/api/v1/cashier/today/settlement",
        json={"cash_total": "50.00", "bank_total": "50.00"},
        headers=stale_headers,
    )
    assert settlement_response.status_code == 409
    assert settlement_response.json()["error"]["code"] == "CASHIER_EDIT_WINDOW_CLOSED"


def test_cashier_can_view_past_days_but_not_future_days(
    client: TestClient, seeded: dict[str, object]
) -> None:
    login(client, "0500000101")
    today = seeded["today"]
    past_date = today - timedelta(days=1)  # type: ignore[operator]
    future_date = today + timedelta(days=1)  # type: ignore[operator]

    past_response = client.get(
        "/api/v1/cashier/today",
        params={"work_date": past_date.isoformat()},
    )
    assert past_response.status_code == 200, past_response.text
    past_day = past_response.json()["data"]
    assert past_day["work_date"] == past_date.isoformat()
    assert all(row["can_edit"] is False for row in past_day["employees"])
    if past_day["settlement"] is not None:
        assert past_day["settlement"]["can_edit"] is False

    future_response = client.get(
        "/api/v1/cashier/today",
        params={"work_date": future_date.isoformat()},
    )
    assert future_response.status_code == 422
    assert future_response.json()["error"]["code"] == "FUTURE_WORK_DATE_NOT_ALLOWED"


def test_cashier_day_becomes_complete_only_after_all_sales_and_settlement(
    client: TestClient, seeded: dict[str, object]
) -> None:
    employees = seeded["employees"]
    assert isinstance(employees, list)
    csrf = login(client, "0500000101")
    initial = client.get("/api/v1/cashier/today").json()["data"]
    assert initial["completion"]["status"] == "incomplete"
    assert initial["completion"]["missing_parts"] == ["employee_sales", "settlement"]

    for employee, services, sales in [
        (employees[0], 4, "400.00"),
        (employees[1], 0, "0.00"),
    ]:
        assert isinstance(employee, Employee)
        response = client.put(
            f"/api/v1/cashier/today/employees/{employee.id}/sale",
            json={"service_count": services, "sales_total": sales},
            headers=cashier_headers(csrf, seeded["today"]),  # type: ignore[arg-type]
        )
        assert response.status_code == 200, response.text

    before_settlement = client.get("/api/v1/cashier/today").json()["data"]
    assert before_settlement["completion"]["employees_complete"] is True
    assert before_settlement["completion"]["settlement_complete"] is False

    response = client.put(
        "/api/v1/cashier/today/settlement",
        json={"cash_total": "200.00", "bank_total": "200.00"},
        headers=cashier_headers(csrf, seeded["today"]),  # type: ignore[arg-type]
    )
    assert response.status_code == 200, response.text
    saved = response.json()["data"]
    assert saved["completion"]["status"] == "complete"
    assert saved["settlement"]["branch_income_total"] == "400.00"
    assert saved["settlement"]["employees_sales_total"] == "400.00"
    assert saved["settlement"]["reconciliation"] == {
        "status": "matched",
        "difference_amount": "0.00",
        "is_provisional": False,
    }


def test_provisional_difference_allows_save_but_blocks_cashier_note(
    client: TestClient, seeded: dict[str, object]
) -> None:
    employee = seeded["employees"][0]  # type: ignore[index]
    assert isinstance(employee, Employee)
    csrf = login(client, "0500000101")
    sale_response = client.put(
        f"/api/v1/cashier/today/employees/{employee.id}/sale",
        json={"service_count": 5, "sales_total": "500.00"},
        headers=cashier_headers(csrf, seeded["today"]),  # type: ignore[arg-type]
    )
    assert sale_response.status_code == 200
    settlement_response = client.put(
        "/api/v1/cashier/today/settlement",
        json={"cash_total": "300.00", "bank_total": "300.00"},
        headers=cashier_headers(csrf, seeded["today"]),  # type: ignore[arg-type]
    )
    assert settlement_response.status_code == 200
    reconciliation = settlement_response.json()["data"]["settlement"]["reconciliation"]
    assert reconciliation["status"] == "surplus"
    assert reconciliation["difference_amount"] == "100.00"
    assert reconciliation["is_provisional"] is True
    note_response = client.patch(
        "/api/v1/cashier/today/settlement/note",
        json={"reconciliation_note": "ملاحظة مبكرة"},
        headers=cashier_headers(csrf, seeded["today"]),  # type: ignore[arg-type]
    )
    assert note_response.status_code == 409
    assert note_response.json()["error"]["code"] == "RECONCILIATION_PROVISIONAL"


def test_admin_edit_locks_employee_record_but_keeps_settlement_open(
    client: TestClient, seeded: dict[str, object]
) -> None:
    employee = seeded["employees"][0]  # type: ignore[index]
    today = seeded["today"]
    branch = seeded["branch"]
    assert isinstance(employee, Employee)
    cashier_csrf = login(client, "0500000101")
    assert (
        client.put(
            f"/api/v1/cashier/today/employees/{employee.id}/sale",
            json={"service_count": 4, "sales_total": "400.00"},
            headers=cashier_headers(cashier_csrf, today),  # type: ignore[arg-type]
        ).status_code
        == 200
    )
    client.cookies.clear()
    admin_csrf = login(client, "0500000001")
    admin_response = client.put(
        f"/api/v1/admin/branches/{branch.id}/days/{today}/employees/{employee.id}/sale",  # type: ignore[union-attr]
        json={"service_count": 5, "sales_total": "450.00", "reason": "تصحيح موثق"},
        headers={"X-CSRF-Token": admin_csrf, "Origin": "http://127.0.0.1:4173"},
    )
    assert admin_response.status_code == 200, admin_response.text
    client.cookies.clear()
    cashier_csrf = login(client, "0500000101")
    locked_response = client.put(
        f"/api/v1/cashier/today/employees/{employee.id}/sale",
        json={"service_count": 6, "sales_total": "500.00"},
        headers=cashier_headers(cashier_csrf, today),  # type: ignore[arg-type]
    )
    assert locked_response.status_code == 409
    assert locked_response.json()["error"]["code"] == "ADMIN_LOCKED_RECORD"
    settlement_response = client.put(
        "/api/v1/cashier/today/settlement",
        json={"cash_total": "250.00", "bank_total": "250.00"},
        headers=cashier_headers(cashier_csrf, today),  # type: ignore[arg-type]
    )
    assert settlement_response.status_code == 200


def test_employee_total_is_stored_after_each_sale(
    db_session: Session, seeded: dict[str, object]
) -> None:
    cashier = seeded["cashier"]
    branch = seeded["branch"]
    employee = seeded["employees"][0]  # type: ignore[index]
    today = seeded["today"]
    assert isinstance(cashier, User)
    assert isinstance(employee, Employee)
    from app.services.business import upsert_sale

    _, settlement = upsert_sale(
        db_session,
        actor=cashier,
        branch_id=branch.id,  # type: ignore[union-attr]
        employee_id=employee.id,
        work_date=today,  # type: ignore[arg-type]
        service_count=3,
        sales_total=Decimal("275.50"),
        confirm_zero_mismatch=False,
    )
    database_sum = db_session.scalar(select(DailySale.sales_total))
    assert money(settlement.employees_sales_total) == money(database_sum)


def test_cashier_day_contract_hides_admin_metadata(
    client: TestClient, seeded: dict[str, object]
) -> None:
    csrf = login(client, "0500000101")
    employee = seeded["employees"][0]  # type: ignore[index]
    assert isinstance(employee, Employee)
    day = client.get("/api/v1/cashier/today").json()["data"]
    assert day["timezone"] == "Asia/Riyadh"
    assert day["totals"] == {"service_count": 0, "employees_sales_total": "0.00"}
    assert day["employees"][0]["record_status"] == "missing"
    assert day["employees"][0]["sale"] is None
    assert day["employees"][0]["can_edit"] is True

    response = client.put(
        f"/api/v1/cashier/today/employees/{employee.id}/sale",
        json={"service_count": 2, "sales_total": "125.50"},
        headers=cashier_headers(csrf, seeded["today"]),  # type: ignore[arg-type]
    )
    assert response.status_code == 200
    row = response.json()["data"]["employee_record"]
    assert row["record_status"] == "saved"
    assert row["sale"]["sales_total"] == "125.50"
    assert "created_by" not in row["sale"]
    assert "admin_updated_by" not in row["sale"]


def test_admin_dashboard_and_branch_day_match_approved_contract(
    client: TestClient, seeded: dict[str, object], db_session: Session
) -> None:
    employees = seeded["employees"]
    assert isinstance(employees, list)
    today = seeded["today"]
    cashier_csrf = login(client, "0500000101")
    for employee in employees:
        assert isinstance(employee, Employee)
        response = client.put(
            f"/api/v1/cashier/today/employees/{employee.id}/sale",
            json={"service_count": 1, "sales_total": "100.00"},
            headers=cashier_headers(cashier_csrf, today),  # type: ignore[arg-type]
        )
        assert response.status_code == 200
    response = client.put(
        "/api/v1/cashier/today/settlement",
        json={"cash_total": "100.00", "bank_total": "100.00"},
        headers=cashier_headers(cashier_csrf, today),  # type: ignore[arg-type]
    )
    assert response.status_code == 200

    primary = seeded["primary"]
    assert isinstance(primary, User)
    pending_branch = Branch(name="فرع دون تسوية")
    db_session.add(pending_branch)
    db_session.flush()
    pending_employee = Employee(employee_code="E0099", name="عاملة دون تسوية")
    db_session.add(pending_employee)
    db_session.flush()
    db_session.add_all(
        [
            BranchStatusPeriod(
                branch_id=pending_branch.id,
                status=EntityStatus.ACTIVE,
                effective_from=today,  # type: ignore[arg-type]
                created_by_user_id=primary.id,
            ),
            EmployeeBranchAssignment(
                employee_id=pending_employee.id,
                branch_id=pending_branch.id,
                assignment_status=EntityStatus.ACTIVE,
                effective_from=today,  # type: ignore[arg-type]
                created_by_user_id=primary.id,
            ),
            DailySale(
                branch_id=pending_branch.id,
                employee_id=pending_employee.id,
                work_date=today,  # type: ignore[arg-type]
                service_count=1,
                sales_total=Decimal("1000.00"),
                created_by_user_id=primary.id,
            ),
            BranchDailySettlement(
                branch_id=pending_branch.id,
                work_date=today,  # type: ignore[arg-type]
                employees_sales_total=Decimal("1000.00"),
            ),
        ]
    )
    db_session.commit()

    client.cookies.clear()
    login(client, "0500000001")
    dashboard = client.get("/api/v1/admin/dashboard").json()["data"]
    assert dashboard["generated_at"].endswith("Z")
    assert dashboard["totals"]["employees_sales_total"] == "1200.00"
    assert "employee_sales_total" not in dashboard["totals"]
    assert dashboard["totals"]["difference_amount"] == "0.00"
    assert dashboard["branches"][0]["settlement_submitted"] is True
    assert dashboard["branches"][0]["has_reconciliation_note"] is False
    assert dashboard["top_employees"][0]["name"] == "عاملة دون تسوية"
    assert dashboard["top_branches"][0]["sales_total"] == "1000.00"

    branch = seeded["branch"]
    detail = client.get(
        f"/api/v1/admin/branches/{branch.id}/days/{today}"  # type: ignore[union-attr]
    ).json()["data"]
    assert detail["totals"] == {
        "service_count": 2,
        "employees_sales_total": "200.00",
    }
    assert detail["employees"][0]["required_for_day"] is True
    assert detail["employees"][0]["record_status"] == "saved"
    assert detail["employees"][0]["sale"]["created_by"]["display_name"]
