from datetime import timedelta

from fastapi.testclient import TestClient

from app.models.entities import Branch
from tests.conftest import login


def admin_headers(csrf: str) -> dict[str, str]:
    return {
        "X-CSRF-Token": csrf,
        "Origin": "http://127.0.0.1:4173",
    }


def test_master_lists_include_effective_periods_assignments_and_only_cashiers(
    client: TestClient, seeded: dict[str, object]
) -> None:
    login(client, "0500000001")
    today = seeded["today"]

    branches = client.get("/api/v1/admin/branches?status=all&page_size=100")
    assert branches.status_code == 200, branches.text
    branch = branches.json()["data"][0]
    assert branch["effective_from"] == (today - timedelta(days=30)).isoformat()
    assert branch["active_cashier"]["display_name"] == "كاشيرة الاختبار"

    employees = client.get("/api/v1/admin/employees?status=all&page_size=100")
    assert employees.status_code == 200, employees.text
    employee = employees.json()["data"][0]
    assert employee["assignments"]
    assert employee["assignments"][0]["branch"]["id"] == branch["id"]
    assert employee["assignments"][0]["created_by_user_id"]

    cashiers = client.get("/api/v1/admin/cashiers?status=all&page_size=100")
    assert cashiers.status_code == 200, cashiers.text
    cashier_rows = cashiers.json()["data"]
    assert [row["phone"] for row in cashier_rows] == ["0500000101"]
    assert all(row["role"] == "cashier" for row in cashier_rows)


def test_created_employee_returns_the_same_assignment_contract(
    client: TestClient, seeded: dict[str, object]
) -> None:
    branch = seeded["branch"]
    today = seeded["today"]
    assert isinstance(branch, Branch)
    csrf = login(client, "0500000001")

    response = client.post(
        "/api/v1/admin/employees",
        json={"name": "سارة", "branch_id": branch.id, "effective_date": today.isoformat()},
        headers=admin_headers(csrf),
    )

    assert response.status_code == 201, response.text
    employee = response.json()["data"]
    assert employee["employee_code"] == "E0003"
    assert employee["effective_from"] == today.isoformat()
    assert employee["assignments"][0]["branch"]["id"] == branch.id
    assert employee["assignments"][0]["status"] == "active"


def test_cashier_creation_enforces_global_phone_and_one_active_cashier_per_branch(
    client: TestClient, seeded: dict[str, object]
) -> None:
    branch = seeded["branch"]
    assert isinstance(branch, Branch)
    csrf = login(client, "0500000001")
    base_payload = {
        "display_name": "كاشيرة جديدة",
        "pin": "654321",
        "branch_id": branch.id,
        "preferred_language": "ar",
    }

    duplicate_phone = client.post(
        "/api/v1/admin/cashiers",
        json={**base_payload, "phone": "0500000001"},
        headers=admin_headers(csrf),
    )
    assert duplicate_phone.status_code == 409
    assert duplicate_phone.json()["error"]["code"] == "PHONE_ALREADY_EXISTS"

    occupied_branch = client.post(
        "/api/v1/admin/cashiers",
        json={**base_payload, "phone": "0500000202"},
        headers=admin_headers(csrf),
    )
    assert occupied_branch.status_code == 409
    assert occupied_branch.json()["error"]["code"] == "ACTIVE_CASHIER_ALREADY_EXISTS"


def test_administrator_management_is_primary_admin_only(
    client: TestClient,
) -> None:
    csrf = login(client, "0500000001")
    created = client.post(
        "/api/v1/admin/administrators",
        json={
            "display_name": "مديرة فرعية",
            "phone": "0500000303",
            "pin": "654321",
            "branch_id": None,
            "preferred_language": "ar",
        },
        headers=admin_headers(csrf),
    )
    assert created.status_code == 201, created.text
    assert created.json()["data"]["is_primary_admin"] is False

    client.cookies.clear()
    login(client, "0500000303", "654321")
    forbidden = client.get("/api/v1/admin/administrators")
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "FORBIDDEN"


def test_audit_list_contains_old_and_new_values_after_admin_change(
    client: TestClient, seeded: dict[str, object]
) -> None:
    branch = seeded["branch"]
    assert isinstance(branch, Branch)
    csrf = login(client, "0500000001")

    renamed = client.patch(
        f"/api/v1/admin/branches/{branch.id}",
        json={"name": "الفرع الجديد"},
        headers=admin_headers(csrf),
    )
    assert renamed.status_code == 200, renamed.text

    response = client.get("/api/v1/admin/audit-logs?event_type=branch.updated&page_size=100")
    assert response.status_code == 200, response.text
    entry = response.json()["data"][0]
    assert entry["actor"]["display_name"] == "مديرة الاختبار"
    assert entry["old_values"]["name"] == "فرع الاختبار"
    assert entry["new_values"]["name"] == "الفرع الجديد"
