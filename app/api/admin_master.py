from datetime import date, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.api.deps import (
    AdminUser,
    DbSession,
    PrimaryAdminUser,
    envelope,
    require_csrf,
)
from app.core.errors import BusinessError
from app.core.security import hash_pin
from app.core.time import iso_utc, riyadh_today, utc_now
from app.models.entities import (
    AuditLog,
    Branch,
    BranchClosureDay,
    BranchDailySettlement,
    BranchStatusPeriod,
    ClosureStatus,
    DailySale,
    Employee,
    EmployeeBranchAssignment,
    EntityStatus,
    Language,
    RefreshSession,
    User,
    UserRole,
)
from app.schemas.requests import (
    AccountCreateRequest,
    AccountReactivateRequest,
    AccountTransferRequest,
    AccountUpdateRequest,
    BranchCreateRequest,
    BranchStatusRequest,
    ClosureCreateRequest,
    EmployeeCreateRequest,
    EmployeeDisableRequest,
    EmployeeReactivateRequest,
    EmployeeTransferRequest,
    NameUpdateRequest,
    PinResetRequest,
    ReasonRequest,
)
from app.services.business import (
    add_audit,
    assignment_at,
    branch_status_at,
    branch_summary,
    close_open_period,
    user_summary,
)

router = APIRouter(prefix="/admin", tags=["Administration master data"])


def paginate(items: list[Any], page: int, page_size: int) -> tuple[list[Any], dict[str, int]]:
    total = len(items)
    start = (page - 1) * page_size
    return items[start : start + page_size], {
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    }


def ensure_employee_effective_date(value: date) -> None:
    today = riyadh_today()
    if value not in {today, today + timedelta(days=1)}:
        raise BusinessError(
            422,
            "INVALID_EFFECTIVE_DATE",
            "Employee changes can take effect today or tomorrow only.",
        )


def account_view(db: DbSession, user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "display_name": user.display_name,
        "phone": user.phone,
        "role": user.role.value,
        "is_primary_admin": user.is_primary_admin,
        "status": user.status.value,
        "preferred_language": user.preferred_language.value,
        "branch": branch_summary(db, user.branch) if user.branch else None,
        "failed_login_attempts": user.failed_login_attempts,
        "locked_until": iso_utc(user.locked_until),
        "last_login_at": iso_utc(user.last_login_at),
        "created_at": iso_utc(user.created_at),
        "updated_at": iso_utc(user.updated_at),
    }


def assignment_view(db: DbSession, assignment: EmployeeBranchAssignment) -> dict[str, Any]:
    return {
        "id": assignment.id,
        "branch": branch_summary(db, assignment.branch, assignment.effective_from),
        "status": assignment.assignment_status.value,
        "effective_from": assignment.effective_from.isoformat(),
        "effective_to": assignment.effective_to.isoformat() if assignment.effective_to else None,
        "created_by_user_id": assignment.created_by_user_id,
        "created_at": iso_utc(assignment.created_at),
    }


@router.get("/branches")
def branches_list(
    request: Request,
    user: AdminUser,
    db: DbSession,
    search: str = "",
    status_filter: Literal["active", "disabled", "all"] = Query("all", alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
) -> dict[str, object]:
    del user
    today = riyadh_today()
    rows = []
    for branch in db.scalars(select(Branch).order_by(Branch.name)):
        current_period = db.scalar(
            select(BranchStatusPeriod)
            .where(
                BranchStatusPeriod.branch_id == branch.id,
                BranchStatusPeriod.effective_from <= today,
                or_(
                    BranchStatusPeriod.effective_to.is_(None),
                    BranchStatusPeriod.effective_to >= today,
                ),
            )
            .order_by(BranchStatusPeriod.effective_from.desc())
            .limit(1)
        )
        current = current_period.status if current_period else EntityStatus.DISABLED
        if status_filter != "all" and current.value != status_filter:
            continue
        if search and search.casefold() not in branch.name.casefold():
            continue
        next_period = db.scalar(
            select(BranchStatusPeriod)
            .where(
                BranchStatusPeriod.branch_id == branch.id,
                BranchStatusPeriod.effective_from > today,
            )
            .order_by(BranchStatusPeriod.effective_from)
            .limit(1)
        )
        employees_count = int(
            db.scalar(
                select(func.count(EmployeeBranchAssignment.id)).where(
                    EmployeeBranchAssignment.branch_id == branch.id,
                    EmployeeBranchAssignment.assignment_status == EntityStatus.ACTIVE,
                    EmployeeBranchAssignment.effective_from <= today,
                    or_(
                        EmployeeBranchAssignment.effective_to.is_(None),
                        EmployeeBranchAssignment.effective_to >= today,
                    ),
                )
            )
            or 0
        )
        cashier = db.scalar(
            select(User).where(
                User.role == UserRole.CASHIER,
                User.status == EntityStatus.ACTIVE,
                User.branch_id == branch.id,
            )
        )
        rows.append(
            {
                "id": branch.id,
                "name": branch.name,
                "status": current.value,
                "effective_from": (
                    current_period.effective_from.isoformat() if current_period else None
                ),
                "active_employees_count": employees_count,
                "active_cashier": user_summary(cashier),
                "scheduled_change": (
                    {
                        "status": next_period.status.value,
                        "effective_date": next_period.effective_from.isoformat(),
                    }
                    if next_period
                    else None
                ),
                "created_at": iso_utc(branch.created_at),
            }
        )
    page_rows, meta = paginate(rows, page, page_size)
    return envelope(request, page_rows, meta=meta)


@router.post("/branches", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_csrf)])
def create_branch(
    payload: BranchCreateRequest,
    request: Request,
    user: AdminUser,
    db: DbSession,
) -> dict[str, object]:
    if payload.effective_date < riyadh_today():
        raise BusinessError(422, "INVALID_EFFECTIVE_DATE", "The effective date cannot be past.")
    branch = Branch(name=payload.name)
    db.add(branch)
    db.flush()
    db.add(
        BranchStatusPeriod(
            branch_id=branch.id,
            status=EntityStatus.ACTIVE,
            effective_from=payload.effective_date,
            created_by_user_id=user.id,
        )
    )
    add_audit(
        db,
        actor=user,
        event_type="branch.created",
        entity_type="branch",
        entity_id=branch.id,
        branch_id=branch.id,
        new_values={"name": branch.name, "effective_date": payload.effective_date.isoformat()},
    )
    db.commit()
    return envelope(request, branch_summary(db, branch, payload.effective_date))


@router.get("/branches/{branch_id}")
def branch_details(
    branch_id: int, request: Request, user: AdminUser, db: DbSession
) -> dict[str, object]:
    del user
    branch = db.get(Branch, branch_id)
    if branch is None:
        raise BusinessError(404, "BRANCH_NOT_FOUND", "Branch not found.")
    periods = list(
        db.scalars(
            select(BranchStatusPeriod)
            .where(BranchStatusPeriod.branch_id == branch_id)
            .order_by(BranchStatusPeriod.effective_from.desc())
        )
    )
    return envelope(
        request,
        {
            **branch_summary(db, branch),
            "created_at": iso_utc(branch.created_at),
            "updated_at": iso_utc(branch.updated_at),
            "status_periods": [
                {
                    "id": item.id,
                    "status": item.status.value,
                    "effective_from": item.effective_from.isoformat(),
                    "effective_to": item.effective_to.isoformat() if item.effective_to else None,
                    "created_by_user_id": item.created_by_user_id,
                }
                for item in periods
            ],
        },
    )


@router.patch("/branches/{branch_id}", dependencies=[Depends(require_csrf)])
def rename_branch(
    branch_id: int,
    payload: NameUpdateRequest,
    request: Request,
    user: AdminUser,
    db: DbSession,
) -> dict[str, object]:
    branch = db.get(Branch, branch_id)
    if branch is None:
        raise BusinessError(404, "BRANCH_NOT_FOUND", "Branch not found.")
    old_name = branch.name
    branch.name = payload.name
    add_audit(
        db,
        actor=user,
        event_type="branch.updated",
        entity_type="branch",
        entity_id=branch.id,
        branch_id=branch.id,
        old_values={"name": old_name},
        new_values={"name": branch.name},
    )
    db.commit()
    return envelope(request, branch_summary(db, branch))


@router.post("/branches/{branch_id}/status-changes", dependencies=[Depends(require_csrf)])
def change_branch_status(
    branch_id: int,
    payload: BranchStatusRequest,
    request: Request,
    user: AdminUser,
    db: DbSession,
) -> dict[str, object]:
    branch = db.get(Branch, branch_id)
    if branch is None:
        raise BusinessError(404, "BRANCH_NOT_FOUND", "Branch not found.")
    if payload.effective_date < riyadh_today():
        raise BusinessError(422, "INVALID_EFFECTIVE_DATE", "The effective date cannot be past.")
    periods = list(
        db.scalars(
            select(BranchStatusPeriod)
            .where(BranchStatusPeriod.branch_id == branch_id)
            .order_by(BranchStatusPeriod.effective_from)
            .with_for_update()
        )
    )
    if any(item.effective_from == payload.effective_date for item in periods):
        raise BusinessError(
            409, "EFFECTIVE_PERIOD_CONFLICT", "A change already exists on this date."
        )
    previous = next(
        (item for item in reversed(periods) if item.effective_from < payload.effective_date), None
    )
    if previous and (
        previous.effective_to is None or previous.effective_to >= payload.effective_date
    ):
        close_open_period(previous, payload.effective_date)
    period = BranchStatusPeriod(
        branch_id=branch.id,
        status=EntityStatus(payload.status),
        effective_from=payload.effective_date,
        created_by_user_id=user.id,
    )
    db.add(period)
    current_status = branch_status_at(db, branch.id, riyadh_today())
    add_audit(
        db,
        actor=user,
        event_type="branch.status_changed",
        entity_type="branch",
        entity_id=branch.id,
        branch_id=branch.id,
        old_values={"status": current_status.value if current_status else None},
        new_values={"status": payload.status, "effective_date": payload.effective_date.isoformat()},
        reason=payload.note,
    )
    db.commit()
    return envelope(
        request,
        {"status": period.status.value, "effective_date": period.effective_from.isoformat()},
    )


@router.get("/employees")
def employees_list(
    request: Request,
    user: AdminUser,
    db: DbSession,
    search: str = "",
    branch_id: int | None = None,
    status_filter: Literal["active", "disabled", "all"] = Query("all", alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
) -> dict[str, object]:
    del user
    today = riyadh_today()
    rows = []
    for employee in db.scalars(select(Employee).order_by(Employee.name)):
        assignment = assignment_at(db, employee.id, today)
        current_status = assignment.assignment_status if assignment else EntityStatus.DISABLED
        if status_filter != "all" and current_status.value != status_filter:
            continue
        if branch_id and (assignment is None or assignment.branch_id != branch_id):
            continue
        if (
            search
            and search.casefold() not in f"{employee.name} {employee.employee_code}".casefold()
        ):
            continue
        assignments = list(
            db.scalars(
                select(EmployeeBranchAssignment)
                .where(EmployeeBranchAssignment.employee_id == employee.id)
                .order_by(EmployeeBranchAssignment.effective_from.desc())
            )
        )
        rows.append(
            {
                "id": employee.id,
                "employee_code": employee.employee_code,
                "name": employee.name,
                "status": current_status.value,
                "branch": branch_summary(db, assignment.branch, today) if assignment else None,
                "effective_from": assignment.effective_from.isoformat() if assignment else None,
                "assignments": [assignment_view(db, item) for item in assignments],
            }
        )
    page_rows, meta = paginate(rows, page, page_size)
    return envelope(request, page_rows, meta=meta)


@router.post(
    "/employees", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_csrf)]
)
def create_employee(
    payload: EmployeeCreateRequest,
    request: Request,
    user: AdminUser,
    db: DbSession,
) -> dict[str, object]:
    ensure_employee_effective_date(payload.effective_date)
    branch = db.get(Branch, payload.branch_id)
    if (
        branch is None
        or branch_status_at(db, branch.id, payload.effective_date) != EntityStatus.ACTIVE
    ):
        raise BusinessError(409, "BRANCH_DISABLED", "The target branch is not active.")
    next_number = int(db.scalar(select(func.coalesce(func.max(Employee.id), 0))) or 0) + 1
    employee = Employee(employee_code=f"E{next_number:04d}", name=payload.name)
    db.add(employee)
    db.flush()
    assignment = EmployeeBranchAssignment(
        employee_id=employee.id,
        branch_id=branch.id,
        assignment_status=EntityStatus.ACTIVE,
        effective_from=payload.effective_date,
        created_by_user_id=user.id,
    )
    db.add(assignment)
    add_audit(
        db,
        actor=user,
        event_type="employee.created",
        entity_type="employee",
        entity_id=employee.id,
        branch_id=branch.id,
        new_values={
            "name": employee.name,
            "employee_code": employee.employee_code,
            "effective_date": payload.effective_date.isoformat(),
        },
    )
    db.commit()
    return envelope(
        request,
        {
            "id": employee.id,
            "employee_code": employee.employee_code,
            "name": employee.name,
            "status": "active",
            "branch": branch_summary(db, branch, payload.effective_date),
            "effective_from": assignment.effective_from.isoformat(),
            "assignments": [assignment_view(db, assignment)],
        },
    )


@router.get("/employees/{employee_id}")
def employee_details(
    employee_id: int, request: Request, user: AdminUser, db: DbSession
) -> dict[str, object]:
    del user
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise BusinessError(404, "EMPLOYEE_NOT_FOUND", "Employee not found.")
    assignments = list(
        db.scalars(
            select(EmployeeBranchAssignment)
            .where(EmployeeBranchAssignment.employee_id == employee_id)
            .order_by(EmployeeBranchAssignment.effective_from.desc())
        )
    )
    current = assignment_at(db, employee.id, riyadh_today())
    return envelope(
        request,
        {
            "id": employee.id,
            "employee_code": employee.employee_code,
            "name": employee.name,
            "status": current.assignment_status.value if current else "disabled",
            "branch": branch_summary(db, current.branch) if current else None,
            "effective_from": current.effective_from.isoformat() if current else None,
            "assignments": [assignment_view(db, item) for item in assignments],
            "created_at": iso_utc(employee.created_at),
            "updated_at": iso_utc(employee.updated_at),
        },
    )


@router.patch("/employees/{employee_id}", dependencies=[Depends(require_csrf)])
def rename_employee(
    employee_id: int,
    payload: NameUpdateRequest,
    request: Request,
    user: AdminUser,
    db: DbSession,
) -> dict[str, object]:
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise BusinessError(404, "EMPLOYEE_NOT_FOUND", "Employee not found.")
    old_name = employee.name
    employee.name = payload.name
    current = assignment_at(db, employee.id, riyadh_today())
    add_audit(
        db,
        actor=user,
        event_type="employee.updated",
        entity_type="employee",
        entity_id=employee.id,
        branch_id=current.branch_id if current else None,
        old_values={"name": old_name},
        new_values={"name": employee.name},
    )
    db.commit()
    return envelope(request, {"id": employee.id, "name": employee.name})


def transition_employee(
    db: DbSession,
    *,
    employee: Employee,
    actor: User,
    target_branch_id: int,
    target_status: EntityStatus,
    effective_date: date,
    event_type: str,
) -> EmployeeBranchAssignment:
    ensure_employee_effective_date(effective_date)
    branch = db.get(Branch, target_branch_id)
    if (
        branch is None
        or branch_status_at(db, target_branch_id, effective_date) != EntityStatus.ACTIVE
    ):
        raise BusinessError(409, "BRANCH_DISABLED", "The target branch is not active.")
    current = assignment_at(db, employee.id, effective_date)
    if (
        current
        and current.branch_id == target_branch_id
        and current.assignment_status == target_status
    ):
        raise BusinessError(
            409, "EFFECTIVE_PERIOD_CONFLICT", "The requested state is already active."
        )
    latest = db.scalar(
        select(EmployeeBranchAssignment)
        .where(EmployeeBranchAssignment.employee_id == employee.id)
        .order_by(EmployeeBranchAssignment.effective_from.desc())
        .with_for_update()
        .limit(1)
    )
    if latest and latest.effective_from >= effective_date:
        raise BusinessError(
            409, "EFFECTIVE_PERIOD_CONFLICT", "A change already exists on this date."
        )
    if latest and (latest.effective_to is None or latest.effective_to >= effective_date):
        close_open_period(latest, effective_date)
    next_assignment = EmployeeBranchAssignment(
        employee_id=employee.id,
        branch_id=target_branch_id,
        assignment_status=target_status,
        effective_from=effective_date,
        created_by_user_id=actor.id,
    )
    db.add(next_assignment)
    db.flush()
    add_audit(
        db,
        actor=actor,
        event_type=event_type,
        entity_type="employee",
        entity_id=employee.id,
        branch_id=target_branch_id,
        old_values=(
            {"branch_id": latest.branch_id, "status": latest.assignment_status.value}
            if latest
            else None
        ),
        new_values={
            "branch_id": target_branch_id,
            "status": target_status.value,
            "effective_date": effective_date.isoformat(),
        },
    )
    return next_assignment


@router.post("/employees/{employee_id}/transfer", dependencies=[Depends(require_csrf)])
def transfer_employee(
    employee_id: int,
    payload: EmployeeTransferRequest,
    request: Request,
    user: AdminUser,
    db: DbSession,
) -> dict[str, object]:
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise BusinessError(404, "EMPLOYEE_NOT_FOUND", "Employee not found.")
    assignment = transition_employee(
        db,
        employee=employee,
        actor=user,
        target_branch_id=payload.target_branch_id,
        target_status=EntityStatus.ACTIVE,
        effective_date=payload.effective_date,
        event_type="employee.transferred",
    )
    db.commit()
    return envelope(request, assignment_view(db, assignment))


@router.post("/employees/{employee_id}/disable", dependencies=[Depends(require_csrf)])
def disable_employee(
    employee_id: int,
    payload: EmployeeDisableRequest,
    request: Request,
    user: AdminUser,
    db: DbSession,
) -> dict[str, object]:
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise BusinessError(404, "EMPLOYEE_NOT_FOUND", "Employee not found.")
    current = assignment_at(db, employee.id, payload.effective_date)
    if current is None:
        raise BusinessError(409, "EMPLOYEE_NOT_ASSIGNED", "Employee has no assignment.")
    assignment = transition_employee(
        db,
        employee=employee,
        actor=user,
        target_branch_id=current.branch_id,
        target_status=EntityStatus.DISABLED,
        effective_date=payload.effective_date,
        event_type="employee.disabled",
    )
    db.commit()
    return envelope(request, assignment_view(db, assignment))


@router.post("/employees/{employee_id}/reactivate", dependencies=[Depends(require_csrf)])
def reactivate_employee(
    employee_id: int,
    payload: EmployeeReactivateRequest,
    request: Request,
    user: AdminUser,
    db: DbSession,
) -> dict[str, object]:
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise BusinessError(404, "EMPLOYEE_NOT_FOUND", "Employee not found.")
    assignment = transition_employee(
        db,
        employee=employee,
        actor=user,
        target_branch_id=payload.branch_id,
        target_status=EntityStatus.ACTIVE,
        effective_date=payload.effective_date,
        event_type="employee.reactivated",
    )
    db.commit()
    return envelope(request, assignment_view(db, assignment))


def ensure_phone_available(db: DbSession, phone: str, exclude_id: int | None = None) -> None:
    existing = db.scalar(select(User).where(User.phone == phone, User.id != exclude_id))
    if existing:
        raise BusinessError(409, "PHONE_ALREADY_EXISTS", "The phone number is already used.")


def ensure_active_cashier_slot(
    db: DbSession, branch_id: int, exclude_id: int | None = None
) -> None:
    existing = db.scalar(
        select(User).where(
            User.role == UserRole.CASHIER,
            User.status == EntityStatus.ACTIVE,
            User.branch_id == branch_id,
            User.id != exclude_id,
        )
    )
    if existing:
        raise BusinessError(
            409, "ACTIVE_CASHIER_ALREADY_EXISTS", "The branch already has an active cashier."
        )


@router.get("/cashiers")
def cashiers_list(
    request: Request,
    user: AdminUser,
    db: DbSession,
    branch_id: int | None = None,
    status_filter: Literal["active", "disabled", "all"] = Query("all", alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
) -> dict[str, object]:
    del user
    statement = select(User).where(User.role == UserRole.CASHIER).order_by(User.display_name)
    if branch_id:
        statement = statement.where(User.branch_id == branch_id)
    if status_filter != "all":
        statement = statement.where(User.status == EntityStatus(status_filter))
    rows = [account_view(db, item) for item in db.scalars(statement)]
    page_rows, meta = paginate(rows, page, page_size)
    return envelope(request, page_rows, meta=meta)


@router.post("/cashiers", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_csrf)])
def create_cashier(
    payload: AccountCreateRequest,
    request: Request,
    user: AdminUser,
    db: DbSession,
) -> dict[str, object]:
    if payload.branch_id is None:
        raise BusinessError(422, "BRANCH_REQUIRED", "A branch is required for a cashier.")
    ensure_phone_available(db, payload.phone)
    ensure_active_cashier_slot(db, payload.branch_id)
    if branch_status_at(db, payload.branch_id, riyadh_today()) != EntityStatus.ACTIVE:
        raise BusinessError(409, "BRANCH_DISABLED", "The target branch is disabled.")
    cashier = User(
        display_name=payload.display_name,
        phone=payload.phone,
        pin_hash=hash_pin(payload.pin),
        role=UserRole.CASHIER,
        is_primary_admin=False,
        status=EntityStatus.ACTIVE,
        preferred_language=Language(payload.preferred_language),
        branch_id=payload.branch_id,
    )
    db.add(cashier)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise BusinessError(
            409, "ACTIVE_CASHIER_ALREADY_EXISTS", "The branch already has an active cashier."
        ) from exc
    add_audit(
        db,
        actor=user,
        event_type="cashier.created",
        entity_type="user",
        entity_id=cashier.id,
        branch_id=cashier.branch_id,
        new_values={"display_name": cashier.display_name, "phone": cashier.phone},
    )
    db.commit()
    return envelope(request, account_view(db, cashier))


@router.get("/cashiers/{user_id}")
def cashier_details(
    user_id: int, request: Request, user: AdminUser, db: DbSession
) -> dict[str, object]:
    del user
    cashier = db.get(User, user_id)
    if cashier is None or cashier.role != UserRole.CASHIER:
        raise BusinessError(404, "CASHIER_NOT_FOUND", "Cashier not found.")
    return envelope(request, account_view(db, cashier))


@router.patch("/cashiers/{user_id}", dependencies=[Depends(require_csrf)])
def update_cashier(
    user_id: int,
    payload: AccountUpdateRequest,
    request: Request,
    user: AdminUser,
    db: DbSession,
) -> dict[str, object]:
    cashier = db.get(User, user_id)
    if cashier is None or cashier.role != UserRole.CASHIER:
        raise BusinessError(404, "CASHIER_NOT_FOUND", "Cashier not found.")
    ensure_phone_available(db, payload.phone, cashier.id)
    old = {"display_name": cashier.display_name, "phone": cashier.phone}
    cashier.display_name = payload.display_name
    cashier.phone = payload.phone
    cashier.preferred_language = Language(payload.preferred_language)
    add_audit(
        db,
        actor=user,
        event_type="cashier.updated",
        entity_type="user",
        entity_id=cashier.id,
        branch_id=cashier.branch_id,
        old_values=old,
        new_values={"display_name": cashier.display_name, "phone": cashier.phone},
    )
    db.commit()
    return envelope(request, account_view(db, cashier))


@router.post("/cashiers/{user_id}/transfer", dependencies=[Depends(require_csrf)])
def transfer_cashier(
    user_id: int,
    payload: AccountTransferRequest,
    request: Request,
    user: AdminUser,
    db: DbSession,
) -> dict[str, object]:
    cashier = db.get(User, user_id)
    if cashier is None or cashier.role != UserRole.CASHIER:
        raise BusinessError(404, "CASHIER_NOT_FOUND", "Cashier not found.")
    ensure_active_cashier_slot(db, payload.target_branch_id, cashier.id)
    old_branch = cashier.branch_id
    cashier.branch_id = payload.target_branch_id
    add_audit(
        db,
        actor=user,
        event_type="cashier.transferred",
        entity_type="user",
        entity_id=cashier.id,
        branch_id=cashier.branch_id,
        old_values={"branch_id": old_branch},
        new_values={"branch_id": cashier.branch_id},
    )
    db.commit()
    return envelope(request, account_view(db, cashier))


def set_account_status(
    db: DbSession,
    *,
    target: User,
    actor: User,
    new_status: EntityStatus,
    branch_id: int | None = None,
) -> None:
    if target.is_primary_admin:
        raise BusinessError(
            409, "PRIMARY_ADMIN_PROTECTED", "The primary administrator is protected."
        )
    if target.role == UserRole.CASHIER and new_status == EntityStatus.ACTIVE:
        target_branch = branch_id or target.branch_id
        if target_branch is None:
            raise BusinessError(422, "BRANCH_REQUIRED", "A branch is required.")
        ensure_active_cashier_slot(db, target_branch, target.id)
        target.branch_id = target_branch
    old_status = target.status
    target.status = new_status
    if new_status == EntityStatus.DISABLED:
        db.execute(
            update(RefreshSession)
            .where(RefreshSession.user_id == target.id, RefreshSession.revoked_at.is_(None))
            .values(revoked_at=utc_now())
        )
    action = "reactivated" if new_status == EntityStatus.ACTIVE else "disabled"
    add_audit(
        db,
        actor=actor,
        event_type=f"{target.role.value}.{action}",
        entity_type="user",
        entity_id=target.id,
        branch_id=target.branch_id,
        old_values={"status": old_status.value},
        new_values={"status": new_status.value},
    )


@router.post("/cashiers/{user_id}/disable", dependencies=[Depends(require_csrf)])
def disable_cashier(
    user_id: int, request: Request, user: AdminUser, db: DbSession
) -> dict[str, object]:
    cashier = db.get(User, user_id)
    if cashier is None or cashier.role != UserRole.CASHIER:
        raise BusinessError(404, "CASHIER_NOT_FOUND", "Cashier not found.")
    set_account_status(db, target=cashier, actor=user, new_status=EntityStatus.DISABLED)
    db.commit()
    return envelope(request, account_view(db, cashier))


@router.post("/cashiers/{user_id}/reactivate", dependencies=[Depends(require_csrf)])
def reactivate_cashier(
    user_id: int,
    payload: AccountReactivateRequest,
    request: Request,
    user: AdminUser,
    db: DbSession,
) -> dict[str, object]:
    cashier = db.get(User, user_id)
    if cashier is None or cashier.role != UserRole.CASHIER:
        raise BusinessError(404, "CASHIER_NOT_FOUND", "Cashier not found.")
    set_account_status(
        db,
        target=cashier,
        actor=user,
        new_status=EntityStatus.ACTIVE,
        branch_id=payload.branch_id,
    )
    db.commit()
    return envelope(request, account_view(db, cashier))


@router.post("/cashiers/{user_id}/pin-reset", dependencies=[Depends(require_csrf)])
def reset_cashier_pin(
    user_id: int,
    payload: PinResetRequest,
    request: Request,
    user: AdminUser,
    db: DbSession,
) -> dict[str, object]:
    cashier = db.get(User, user_id)
    if cashier is None or cashier.role != UserRole.CASHIER:
        raise BusinessError(404, "CASHIER_NOT_FOUND", "Cashier not found.")
    cashier.pin_hash = hash_pin(payload.new_pin)
    db.execute(
        update(RefreshSession)
        .where(RefreshSession.user_id == cashier.id, RefreshSession.revoked_at.is_(None))
        .values(revoked_at=utc_now())
    )
    add_audit(
        db,
        actor=user,
        event_type="cashier.pin_reset",
        entity_type="user",
        entity_id=cashier.id,
        branch_id=cashier.branch_id,
    )
    db.commit()
    return envelope(request, {"status": "pin_reset"})


@router.post("/cashiers/{user_id}/unlock", dependencies=[Depends(require_csrf)])
def unlock_cashier(
    user_id: int, request: Request, user: AdminUser, db: DbSession
) -> dict[str, object]:
    cashier = db.get(User, user_id)
    if cashier is None or cashier.role != UserRole.CASHIER:
        raise BusinessError(404, "CASHIER_NOT_FOUND", "Cashier not found.")
    cashier.failed_login_attempts = 0
    cashier.locked_until = None
    add_audit(
        db,
        actor=user,
        event_type="cashier.unlocked",
        entity_type="user",
        entity_id=cashier.id,
        branch_id=cashier.branch_id,
    )
    db.commit()
    return envelope(request, account_view(db, cashier))


@router.get("/administrators")
def administrators_list(
    request: Request, user: PrimaryAdminUser, db: DbSession
) -> dict[str, object]:
    del user
    rows = [
        account_view(db, item)
        for item in db.scalars(
            select(User)
            .where(User.role == UserRole.ADMIN)
            .order_by(User.is_primary_admin.desc(), User.display_name)
        )
    ]
    return envelope(request, rows)


@router.post(
    "/administrators",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_csrf)],
)
def create_administrator(
    payload: AccountCreateRequest,
    request: Request,
    user: PrimaryAdminUser,
    db: DbSession,
) -> dict[str, object]:
    if payload.branch_id is not None:
        raise BusinessError(
            422, "BRANCH_NOT_ALLOWED", "Administrators are not assigned to branches."
        )
    ensure_phone_available(db, payload.phone)
    administrator = User(
        display_name=payload.display_name,
        phone=payload.phone,
        pin_hash=hash_pin(payload.pin),
        role=UserRole.ADMIN,
        is_primary_admin=False,
        status=EntityStatus.ACTIVE,
        preferred_language=Language(payload.preferred_language),
        branch_id=None,
    )
    db.add(administrator)
    db.flush()
    add_audit(
        db,
        actor=user,
        event_type="administrator.created",
        entity_type="user",
        entity_id=administrator.id,
        branch_id=None,
        new_values={"display_name": administrator.display_name, "phone": administrator.phone},
    )
    db.commit()
    return envelope(request, account_view(db, administrator))


def get_administrator(db: DbSession, user_id: int) -> User:
    administrator = db.get(User, user_id)
    if administrator is None or administrator.role != UserRole.ADMIN:
        raise BusinessError(404, "ADMINISTRATOR_NOT_FOUND", "Administrator not found.")
    return administrator


@router.patch("/administrators/{user_id}", dependencies=[Depends(require_csrf)])
def update_administrator(
    user_id: int,
    payload: AccountUpdateRequest,
    request: Request,
    user: PrimaryAdminUser,
    db: DbSession,
) -> dict[str, object]:
    administrator = get_administrator(db, user_id)
    ensure_phone_available(db, payload.phone, administrator.id)
    old = {"display_name": administrator.display_name, "phone": administrator.phone}
    administrator.display_name = payload.display_name
    administrator.phone = payload.phone
    administrator.preferred_language = Language(payload.preferred_language)
    add_audit(
        db,
        actor=user,
        event_type="administrator.updated",
        entity_type="user",
        entity_id=administrator.id,
        branch_id=None,
        old_values=old,
        new_values={
            "display_name": administrator.display_name,
            "phone": administrator.phone,
        },
    )
    db.commit()
    return envelope(request, account_view(db, administrator))


@router.post("/administrators/{user_id}/disable", dependencies=[Depends(require_csrf)])
def disable_administrator(
    user_id: int, request: Request, user: PrimaryAdminUser, db: DbSession
) -> dict[str, object]:
    administrator = get_administrator(db, user_id)
    set_account_status(db, target=administrator, actor=user, new_status=EntityStatus.DISABLED)
    db.commit()
    return envelope(request, account_view(db, administrator))


@router.post("/administrators/{user_id}/reactivate", dependencies=[Depends(require_csrf)])
def reactivate_administrator(
    user_id: int, request: Request, user: PrimaryAdminUser, db: DbSession
) -> dict[str, object]:
    administrator = get_administrator(db, user_id)
    set_account_status(db, target=administrator, actor=user, new_status=EntityStatus.ACTIVE)
    db.commit()
    return envelope(request, account_view(db, administrator))


@router.post("/administrators/{user_id}/pin-reset", dependencies=[Depends(require_csrf)])
def reset_administrator_pin(
    user_id: int,
    payload: PinResetRequest,
    request: Request,
    user: PrimaryAdminUser,
    db: DbSession,
) -> dict[str, object]:
    administrator = get_administrator(db, user_id)
    administrator.pin_hash = hash_pin(payload.new_pin)
    db.execute(
        update(RefreshSession)
        .where(RefreshSession.user_id == administrator.id, RefreshSession.revoked_at.is_(None))
        .values(revoked_at=utc_now())
    )
    add_audit(
        db,
        actor=user,
        event_type="administrator.pin_reset",
        entity_type="user",
        entity_id=administrator.id,
        branch_id=None,
    )
    db.commit()
    return envelope(request, {"status": "pin_reset"})


@router.get("/branch-closures")
def closures_list(
    request: Request,
    user: AdminUser,
    db: DbSession,
    date_from: date | None = None,
    date_to: date | None = None,
    branch_id: int | None = None,
    status_filter: Literal["closed", "reopened", "all"] = Query("all", alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
) -> dict[str, object]:
    del user
    statement = select(BranchClosureDay).order_by(BranchClosureDay.work_date.desc())
    if date_from:
        statement = statement.where(BranchClosureDay.work_date >= date_from)
    if date_to:
        statement = statement.where(BranchClosureDay.work_date <= date_to)
    if branch_id:
        statement = statement.where(BranchClosureDay.branch_id == branch_id)
    if status_filter != "all":
        statement = statement.where(BranchClosureDay.status == ClosureStatus(status_filter))
    rows = [closure_view(db, item) for item in db.scalars(statement)]
    page_rows, meta = paginate(rows, page, page_size)
    return envelope(request, page_rows, meta=meta)


def closure_view(db: DbSession, closure: BranchClosureDay) -> dict[str, Any]:
    return {
        "id": closure.id,
        "branch": branch_summary(db, closure.branch, closure.work_date),
        "work_date": closure.work_date.isoformat(),
        "status": closure.status.value,
        "close_reason": closure.close_reason,
        "closed_by": user_summary(closure.closed_by),
        "closed_at": iso_utc(closure.closed_at),
        "reopen_reason": closure.reopen_reason,
        "reopened_by": user_summary(closure.reopened_by),
        "reopened_at": iso_utc(closure.reopened_at),
    }


@router.post(
    "/branch-closures",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_csrf)],
)
def create_closure(
    payload: ClosureCreateRequest,
    request: Request,
    user: AdminUser,
    db: DbSession,
) -> dict[str, object]:
    branch = db.get(Branch, payload.branch_id)
    if branch is None:
        raise BusinessError(404, "BRANCH_NOT_FOUND", "Branch not found.")
    has_sale = db.scalar(
        select(DailySale.id)
        .where(
            DailySale.branch_id == payload.branch_id,
            DailySale.work_date == payload.work_date,
        )
        .limit(1)
    )
    submitted_settlement = db.scalar(
        select(BranchDailySettlement.id)
        .where(
            BranchDailySettlement.branch_id == payload.branch_id,
            BranchDailySettlement.work_date == payload.work_date,
            BranchDailySettlement.settlement_submitted_at.is_not(None),
        )
        .limit(1)
    )
    if has_sale or submitted_settlement:
        raise BusinessError(409, "DAY_HAS_ACTIVITY", "A day with activity cannot be closed.")
    existing = db.scalar(
        select(BranchClosureDay).where(
            BranchClosureDay.branch_id == payload.branch_id,
            BranchClosureDay.work_date == payload.work_date,
        )
    )
    if existing and existing.status == ClosureStatus.CLOSED:
        raise BusinessError(409, "DAY_ALREADY_CLOSED", "The day is already closed.")
    if existing:
        existing.status = ClosureStatus.CLOSED
        existing.close_reason = payload.reason
        existing.closed_by_user_id = user.id
        existing.closed_at = utc_now()
        existing.reopen_reason = None
        existing.reopened_by_user_id = None
        existing.reopened_at = None
        closure = existing
    else:
        closure = BranchClosureDay(
            branch_id=payload.branch_id,
            work_date=payload.work_date,
            status=ClosureStatus.CLOSED,
            close_reason=payload.reason,
            closed_by_user_id=user.id,
            closed_at=utc_now(),
        )
        db.add(closure)
    db.flush()
    add_audit(
        db,
        actor=user,
        event_type="branch_day.closed",
        entity_type="branch_closure_day",
        entity_id=closure.id,
        branch_id=closure.branch_id,
        new_values={"work_date": closure.work_date.isoformat(), "status": "closed"},
        reason=payload.reason,
    )
    db.commit()
    return envelope(request, closure_view(db, closure))


@router.post("/branch-closures/{closure_id}/reopen", dependencies=[Depends(require_csrf)])
def reopen_closure(
    closure_id: int,
    payload: ReasonRequest,
    request: Request,
    user: AdminUser,
    db: DbSession,
) -> dict[str, object]:
    closure = db.get(BranchClosureDay, closure_id)
    if closure is None:
        raise BusinessError(404, "CLOSURE_NOT_FOUND", "Closure not found.")
    if closure.status == ClosureStatus.REOPENED:
        raise BusinessError(409, "DAY_ALREADY_REOPENED", "The day is already reopened.")
    closure.status = ClosureStatus.REOPENED
    closure.reopen_reason = payload.reason
    closure.reopened_by_user_id = user.id
    closure.reopened_at = utc_now()
    add_audit(
        db,
        actor=user,
        event_type="branch_day.reopened",
        entity_type="branch_closure_day",
        entity_id=closure.id,
        branch_id=closure.branch_id,
        old_values={"status": "closed"},
        new_values={"status": "reopened"},
        reason=payload.reason,
    )
    db.commit()
    return envelope(request, closure_view(db, closure))


def audit_view(entry: AuditLog, *, details: bool) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": entry.id,
        "occurred_at": iso_utc(entry.occurred_at),
        "actor": user_summary(entry.actor),
        "event_type": entry.event_type,
        "entity_type": entry.entity_type,
        "entity_id": entry.entity_id,
        "branch": ({"id": entry.branch.id, "name": entry.branch.name} if entry.branch else None),
        "reason": entry.reason,
    }
    if details:
        data["old_values"] = entry.old_values
        data["new_values"] = entry.new_values
    return data


@router.get("/audit-logs")
def audit_logs(
    request: Request,
    user: AdminUser,
    db: DbSession,
    date_from: date | None = None,
    date_to: date | None = None,
    actor_user_id: int | None = None,
    event_type: str | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    branch_id: int | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
) -> dict[str, object]:
    del user
    statement = select(AuditLog).order_by(AuditLog.occurred_at.desc())
    if date_from:
        statement = statement.where(AuditLog.occurred_at >= date_from)
    if date_to:
        statement = statement.where(AuditLog.occurred_at < date_to + timedelta(days=1))
    if actor_user_id:
        statement = statement.where(AuditLog.actor_user_id == actor_user_id)
    if event_type:
        statement = statement.where(AuditLog.event_type == event_type)
    if entity_type:
        statement = statement.where(AuditLog.entity_type == entity_type)
    if entity_id:
        statement = statement.where(AuditLog.entity_id == entity_id)
    if branch_id:
        statement = statement.where(AuditLog.branch_id == branch_id)
    rows = [audit_view(item, details=True) for item in db.scalars(statement)]
    page_rows, meta = paginate(rows, page, page_size)
    return envelope(request, page_rows, meta=meta)


@router.get("/audit-logs/{audit_log_id}")
def audit_log_details(
    audit_log_id: int, request: Request, user: AdminUser, db: DbSession
) -> dict[str, object]:
    del user
    entry = db.get(AuditLog, audit_log_id)
    if entry is None:
        raise BusinessError(404, "AUDIT_LOG_NOT_FOUND", "Audit log not found.")
    return envelope(request, audit_view(entry, details=True))
