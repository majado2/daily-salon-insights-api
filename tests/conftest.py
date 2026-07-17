from collections.abc import Generator
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.security import hash_pin
from app.core.time import riyadh_today
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.entities import (
    Branch,
    BranchStatusPeriod,
    Employee,
    EmployeeBranchAssignment,
    EntityStatus,
    Language,
    User,
    UserRole,
)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session
    Base.metadata.drop_all(engine)


@pytest.fixture
def seeded(db_session: Session) -> dict[str, object]:
    today = riyadh_today()
    branch = Branch(name="فرع الاختبار")
    db_session.add(branch)
    db_session.flush()
    primary = User(
        display_name="مديرة الاختبار",
        phone="0500000001",
        pin_hash=hash_pin("123456"),
        role=UserRole.ADMIN,
        is_primary_admin=True,
        status=EntityStatus.ACTIVE,
        preferred_language=Language.AR,
    )
    cashier = User(
        display_name="كاشيرة الاختبار",
        phone="0500000101",
        pin_hash=hash_pin("123456"),
        role=UserRole.CASHIER,
        is_primary_admin=False,
        status=EntityStatus.ACTIVE,
        preferred_language=Language.AR,
        branch_id=branch.id,
    )
    db_session.add_all([primary, cashier])
    db_session.flush()
    db_session.add(
        BranchStatusPeriod(
            branch_id=branch.id,
            status=EntityStatus.ACTIVE,
            effective_from=today - timedelta(days=30),
            created_by_user_id=primary.id,
        )
    )
    employees = []
    for code, name in [("E0001", "نوف"), ("E0002", "ريم")]:
        employee = Employee(employee_code=code, name=name)
        db_session.add(employee)
        db_session.flush()
        db_session.add(
            EmployeeBranchAssignment(
                employee_id=employee.id,
                branch_id=branch.id,
                assignment_status=EntityStatus.ACTIVE,
                effective_from=today - timedelta(days=30),
                created_by_user_id=primary.id,
            )
        )
        employees.append(employee)
    db_session.commit()
    return {
        "branch": branch,
        "primary": primary,
        "cashier": cashier,
        "employees": employees,
        "today": today,
    }


@pytest.fixture
def client(db_session: Session, seeded: dict[str, object]) -> Generator[TestClient, None, None]:
    del seeded

    def override_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, base_url="http://testserver") as test_client:
        yield test_client
    app.dependency_overrides.clear()


def login(client: TestClient, phone: str, pin: str = "123456") -> str:
    csrf_response = client.get("/api/v1/auth/csrf")
    assert csrf_response.status_code == 200
    token = csrf_response.json()["data"]["csrf_token"]
    response = client.post(
        "/api/v1/auth/login",
        json={"phone": phone, "pin": pin},
        headers={"X-CSRF-Token": token, "Origin": "http://127.0.0.1:4173"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["csrf_token"]


def cashier_headers(csrf: str, work_date: date) -> dict[str, str]:
    return {
        "X-CSRF-Token": csrf,
        "X-Work-Date": work_date.isoformat(),
        "Origin": "http://127.0.0.1:4173",
    }
