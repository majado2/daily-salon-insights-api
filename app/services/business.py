from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import BusinessError
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
    User,
    UserRole,
)

ZERO = Decimal("0.00")


def money(value: Decimal | None) -> str | None:
    return None if value is None else f"{value.quantize(Decimal('0.01')):.2f}"


def user_summary(user: User | None) -> dict[str, Any] | None:
    if user is None:
        return None
    return {"id": user.id, "display_name": user.display_name}


def branch_status_at(db: Session, branch_id: int, work_date: date) -> EntityStatus | None:
    return db.scalar(
        select(BranchStatusPeriod.status)
        .where(
            BranchStatusPeriod.branch_id == branch_id,
            BranchStatusPeriod.effective_from <= work_date,
            or_(
                BranchStatusPeriod.effective_to.is_(None),
                BranchStatusPeriod.effective_to >= work_date,
            ),
        )
        .order_by(BranchStatusPeriod.effective_from.desc())
        .limit(1)
    )


def branch_summary(db: Session, branch: Branch, work_date: date | None = None) -> dict[str, Any]:
    day = work_date or riyadh_today()
    status = branch_status_at(db, branch.id, day)
    return {"id": branch.id, "name": branch.name, "status": (status or EntityStatus.DISABLED).value}


def assignment_at(
    db: Session, employee_id: int, work_date: date
) -> EmployeeBranchAssignment | None:
    return db.scalar(
        select(EmployeeBranchAssignment)
        .where(
            EmployeeBranchAssignment.employee_id == employee_id,
            EmployeeBranchAssignment.effective_from <= work_date,
            or_(
                EmployeeBranchAssignment.effective_to.is_(None),
                EmployeeBranchAssignment.effective_to >= work_date,
            ),
        )
        .order_by(EmployeeBranchAssignment.effective_from.desc())
        .limit(1)
    )


def required_assignments(
    db: Session, branch_id: int, work_date: date
) -> list[EmployeeBranchAssignment]:
    return list(
        db.scalars(
            select(EmployeeBranchAssignment)
            .where(
                EmployeeBranchAssignment.branch_id == branch_id,
                EmployeeBranchAssignment.assignment_status == EntityStatus.ACTIVE,
                EmployeeBranchAssignment.effective_from <= work_date,
                or_(
                    EmployeeBranchAssignment.effective_to.is_(None),
                    EmployeeBranchAssignment.effective_to >= work_date,
                ),
            )
            .order_by(EmployeeBranchAssignment.employee_id)
        )
    )


def employee_status_at(db: Session, employee_id: int, work_date: date) -> EntityStatus:
    assignment = assignment_at(db, employee_id, work_date)
    return assignment.assignment_status if assignment else EntityStatus.DISABLED


def employee_summary(db: Session, employee: Employee, work_date: date) -> dict[str, Any]:
    return {
        "id": employee.id,
        "employee_code": employee.employee_code,
        "name": employee.name,
        "status": employee_status_at(db, employee.id, work_date).value,
    }


def closure_at(db: Session, branch_id: int, work_date: date) -> BranchClosureDay | None:
    return db.scalar(
        select(BranchClosureDay).where(
            BranchClosureDay.branch_id == branch_id,
            BranchClosureDay.work_date == work_date,
            BranchClosureDay.status == ClosureStatus.CLOSED,
        )
    )


def get_settlement(
    db: Session, branch_id: int, work_date: date, *, for_update: bool = False
) -> BranchDailySettlement | None:
    statement = select(BranchDailySettlement).where(
        BranchDailySettlement.branch_id == branch_id,
        BranchDailySettlement.work_date == work_date,
    )
    if for_update:
        statement = statement.with_for_update()
    return db.scalar(statement)


def get_or_create_settlement(db: Session, branch_id: int, work_date: date) -> BranchDailySettlement:
    settlement = get_settlement(db, branch_id, work_date, for_update=True)
    if settlement is None:
        settlement = BranchDailySettlement(
            branch_id=branch_id,
            work_date=work_date,
            employees_sales_total=ZERO,
            row_created_at=utc_now(),
        )
        db.add(settlement)
        db.flush()
    return settlement


def add_audit(
    db: Session,
    *,
    actor: User | None,
    event_type: str,
    entity_type: str,
    entity_id: int | None,
    branch_id: int | None,
    old_values: dict[str, Any] | None = None,
    new_values: dict[str, Any] | None = None,
    reason: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        actor_user_id=actor.id if actor else None,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        branch_id=branch_id,
        old_values=old_values,
        new_values=new_values,
        reason=reason,
        occurred_at=utc_now(),
    )
    db.add(entry)
    return entry


def completion_view(db: Session, branch_id: int, work_date: date) -> dict[str, Any]:
    required_ids = [item.employee_id for item in required_assignments(db, branch_id, work_date)]
    completed = 0
    if required_ids:
        completed = int(
            db.scalar(
                select(func.count(DailySale.id)).where(
                    DailySale.branch_id == branch_id,
                    DailySale.work_date == work_date,
                    DailySale.employee_id.in_(required_ids),
                )
            )
            or 0
        )
    settlement = get_settlement(db, branch_id, work_date)
    settlement_complete = bool(
        settlement
        and settlement.settlement_submitted_at
        and settlement.cash_total is not None
        and settlement.bank_total is not None
    )
    employees_complete = completed == len(required_ids)
    closed = closure_at(db, branch_id, work_date) is not None
    missing_parts: list[str] = []
    if not employees_complete:
        missing_parts.append("employee_sales")
    if not settlement_complete:
        missing_parts.append("settlement")
    return {
        "status": "closed" if closed else "complete" if not missing_parts else "incomplete",
        "required_employees": len(required_ids),
        "completed_employees": completed,
        "missing_employees": max(0, len(required_ids) - completed),
        "employees_complete": employees_complete,
        "settlement_complete": settlement_complete,
        "missing_parts": [] if closed else missing_parts,
    }


def reconciliation_view(
    db: Session, branch_id: int, work_date: date, settlement: BranchDailySettlement | None = None
) -> dict[str, Any]:
    settlement = settlement or get_settlement(db, branch_id, work_date)
    if settlement is None or settlement.settlement_submitted_at is None:
        return {"status": "pending", "difference_amount": None, "is_provisional": False}
    income = settlement.branch_income_total
    if income is None:
        income = (settlement.cash_total or ZERO) + (settlement.bank_total or ZERO)
    employee_total = settlement.employees_sales_total or ZERO
    difference = abs(income - employee_total)
    if income == employee_total:
        status = "matched"
    elif income > employee_total:
        status = "surplus"
    else:
        status = "shortage"
    completion = completion_view(db, branch_id, work_date)
    return {
        "status": status,
        "difference_amount": money(difference),
        "is_provisional": not completion["employees_complete"],
    }


def ensure_branch_accepts_activity(db: Session, branch_id: int, work_date: date) -> Branch:
    branch = db.get(Branch, branch_id)
    if branch is None:
        raise BusinessError(404, "BRANCH_NOT_FOUND", "Branch not found.")
    if branch_status_at(db, branch_id, work_date) != EntityStatus.ACTIVE:
        raise BusinessError(403, "BRANCH_DISABLED", "The branch is disabled for this work date.")
    if closure_at(db, branch_id, work_date):
        raise BusinessError(409, "DAY_CLOSED", "The branch day is closed.")
    return branch


def ensure_cashier_window(actor: User, branch_id: int, work_date: date) -> None:
    if actor.role != UserRole.CASHIER or actor.branch_id != branch_id:
        raise BusinessError(403, "FORBIDDEN", "You cannot access this branch.")
    if work_date != riyadh_today():
        raise BusinessError(
            409, "CASHIER_EDIT_WINDOW_CLOSED", "Only today's records can be edited."
        )


def recompute_employee_total(db: Session, branch_id: int, work_date: date) -> BranchDailySettlement:
    settlement = get_or_create_settlement(db, branch_id, work_date)
    total = db.scalar(
        select(func.coalesce(func.sum(DailySale.sales_total), ZERO)).where(
            DailySale.branch_id == branch_id,
            DailySale.work_date == work_date,
        )
    )
    settlement.employees_sales_total = Decimal(total or ZERO).quantize(Decimal("0.01"))
    settlement.employees_total_updated_at = utc_now()
    db.flush()
    return settlement


def upsert_sale(
    db: Session,
    *,
    actor: User,
    branch_id: int,
    employee_id: int,
    work_date: date,
    service_count: int,
    sales_total: Decimal,
    confirm_zero_mismatch: bool,
    admin_reason: str | None = None,
) -> tuple[DailySale, BranchDailySettlement]:
    ensure_branch_accepts_activity(db, branch_id, work_date)
    is_admin = actor.role == UserRole.ADMIN
    if not is_admin:
        ensure_cashier_window(actor, branch_id, work_date)
    elif not admin_reason or not admin_reason.strip():
        raise BusinessError(422, "ADMIN_REASON_REQUIRED", "An administrative reason is required.")

    assignment = assignment_at(db, employee_id, work_date)
    if (
        assignment is None
        or assignment.branch_id != branch_id
        or assignment.assignment_status != EntityStatus.ACTIVE
    ):
        raise BusinessError(
            409,
            "EMPLOYEE_NOT_ASSIGNED",
            "The employee is not active in this branch on the selected date.",
        )

    normalized_total = sales_total.quantize(Decimal("0.01"))
    zero_mismatch = (service_count == 0) != (normalized_total == ZERO)
    if zero_mismatch and not confirm_zero_mismatch:
        raise BusinessError(
            409,
            "ZERO_MISMATCH_CONFIRMATION_REQUIRED",
            "Confirm the mismatch between service count and sales total.",
        )

    existing = db.scalar(
        select(DailySale)
        .where(DailySale.employee_id == employee_id, DailySale.work_date == work_date)
        .with_for_update()
    )
    now = utc_now()
    if existing and not is_admin and existing.admin_locked_at is not None:
        raise BusinessError(409, "ADMIN_LOCKED_RECORD", "This record was edited by administration.")
    old_values = (
        {"service_count": existing.service_count, "sales_total": money(existing.sales_total)}
        if existing
        else None
    )
    created = existing is None
    if existing is None:
        existing = DailySale(
            branch_id=branch_id,
            employee_id=employee_id,
            work_date=work_date,
            service_count=service_count,
            sales_total=normalized_total,
            created_by_user_id=actor.id,
            created_at=now,
            updated_at=now,
        )
        db.add(existing)
    else:
        existing.service_count = service_count
        existing.sales_total = normalized_total
        existing.updated_at = now

    if is_admin:
        existing.admin_updated_by_user_id = actor.id
        if existing.admin_locked_at is None:
            existing.admin_locked_at = now
            existing.admin_locked_by_user_id = actor.id
    elif not created:
        existing.cashier_updated_by_user_id = actor.id

    db.flush()
    settlement = recompute_employee_total(db, branch_id, work_date)
    add_audit(
        db,
        actor=actor,
        event_type="daily_sale.created" if created else "daily_sale.updated",
        entity_type="daily_sale",
        entity_id=existing.id,
        branch_id=branch_id,
        old_values=old_values,
        new_values={"service_count": service_count, "sales_total": money(normalized_total)},
        reason=admin_reason.strip() if admin_reason else None,
    )
    db.commit()
    db.refresh(existing)
    db.refresh(settlement)
    return existing, settlement


def upsert_settlement(
    db: Session,
    *,
    actor: User,
    branch_id: int,
    work_date: date,
    cash_total: Decimal,
    bank_total: Decimal,
    reconciliation_note: str | None = None,
    admin_reason: str | None = None,
) -> BranchDailySettlement:
    ensure_branch_accepts_activity(db, branch_id, work_date)
    is_admin = actor.role == UserRole.ADMIN
    if not is_admin:
        ensure_cashier_window(actor, branch_id, work_date)
    elif not admin_reason or not admin_reason.strip():
        raise BusinessError(422, "ADMIN_REASON_REQUIRED", "An administrative reason is required.")

    settlement = get_or_create_settlement(db, branch_id, work_date)
    was_submitted = settlement.settlement_submitted_at is not None
    old_values = {
        "cash_total": money(settlement.cash_total),
        "bank_total": money(settlement.bank_total),
        "reconciliation_note": settlement.reconciliation_note,
    }
    now = utc_now()
    settlement.cash_total = cash_total.quantize(Decimal("0.01"))
    settlement.bank_total = bank_total.quantize(Decimal("0.01"))
    if not was_submitted:
        settlement.settlement_submitted_at = now
        settlement.created_by_user_id = actor.id
    if is_admin:
        settlement.reconciliation_note = reconciliation_note
        settlement.admin_updated_by_user_id = actor.id
        settlement.admin_updated_at = now
    elif was_submitted:
        settlement.cashier_updated_by_user_id = actor.id
        settlement.cashier_updated_at = now
    db.flush()
    add_audit(
        db,
        actor=actor,
        event_type="settlement.created" if not was_submitted else "settlement.updated",
        entity_type="branch_daily_settlement",
        entity_id=settlement.id,
        branch_id=branch_id,
        old_values=old_values if was_submitted else None,
        new_values={
            "cash_total": money(settlement.cash_total),
            "bank_total": money(settlement.bank_total),
            "reconciliation_note": settlement.reconciliation_note,
        },
        reason=admin_reason.strip() if admin_reason else None,
    )
    db.commit()
    db.refresh(settlement)
    return settlement


def update_settlement_note(
    db: Session,
    *,
    actor: User,
    branch_id: int,
    work_date: date,
    note: str | None,
    admin_reason: str | None = None,
) -> BranchDailySettlement:
    settlement = get_settlement(db, branch_id, work_date, for_update=True)
    if settlement is None:
        raise BusinessError(404, "SETTLEMENT_NOT_FOUND", "Settlement not found.")
    is_admin = actor.role == UserRole.ADMIN
    if not is_admin:
        ensure_cashier_window(actor, branch_id, work_date)
        view = reconciliation_view(db, branch_id, work_date, settlement)
        if view["is_provisional"]:
            raise BusinessError(
                409,
                "RECONCILIATION_PROVISIONAL",
                "The note cannot be changed until all employee records are complete.",
            )
        if view["status"] == "pending":
            raise BusinessError(409, "SETTLEMENT_PENDING", "Submit cash and bank totals first.")
        if view["status"] == "matched" and note:
            raise BusinessError(
                409, "NOTE_NOT_ALLOWED_FOR_MATCHED", "Only clearing an existing note is allowed."
            )
    elif not admin_reason or not admin_reason.strip():
        raise BusinessError(422, "ADMIN_REASON_REQUIRED", "An administrative reason is required.")

    old_note = settlement.reconciliation_note
    settlement.reconciliation_note = note.strip() if note and note.strip() else None
    now = utc_now()
    if is_admin:
        settlement.admin_updated_by_user_id = actor.id
        settlement.admin_updated_at = now
    else:
        settlement.cashier_updated_by_user_id = actor.id
        settlement.cashier_updated_at = now
    add_audit(
        db,
        actor=actor,
        event_type="settlement.note_cleared"
        if settlement.reconciliation_note is None
        else "settlement.note_updated",
        entity_type="branch_daily_settlement",
        entity_id=settlement.id,
        branch_id=branch_id,
        old_values={"reconciliation_note": old_note},
        new_values={"reconciliation_note": settlement.reconciliation_note},
        reason=admin_reason.strip() if admin_reason else None,
    )
    db.commit()
    db.refresh(settlement)
    return settlement


def sale_view(db: Session, sale: DailySale, *, cashier: bool) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": sale.id,
        "service_count": sale.service_count,
        "sales_total": money(sale.sales_total),
        "saved_at": iso_utc(sale.updated_at),
        "admin_locked": sale.admin_locked_at is not None,
    }
    if not cashier:
        data.update(
            {
                "created_by": user_summary(sale.created_by),
                "cashier_updated_by": user_summary(sale.cashier_updated_by),
                "admin_updated_by": user_summary(sale.admin_updated_by),
                "created_at": iso_utc(sale.created_at),
                "updated_at": iso_utc(sale.updated_at),
                "admin_locked_at": iso_utc(sale.admin_locked_at),
            }
        )
    return data


def settlement_view(
    db: Session,
    settlement: BranchDailySettlement | None,
    branch_id: int,
    work_date: date,
    *,
    cashier: bool,
) -> dict[str, Any]:
    reconciliation = reconciliation_view(db, branch_id, work_date, settlement)
    base: dict[str, Any] = {
        "cash_total": money(settlement.cash_total) if settlement else None,
        "bank_total": money(settlement.bank_total) if settlement else None,
        "branch_income_total": money(settlement.branch_income_total) if settlement else None,
        "employees_sales_total": money(settlement.employees_sales_total) if settlement else "0.00",
        "reconciliation_note": settlement.reconciliation_note if settlement else None,
        "reconciliation": reconciliation,
    }
    if cashier:
        base.update(
            {
                "submitted_at": iso_utc(settlement.settlement_submitted_at) if settlement else None,
                "can_edit": work_date == riyadh_today(),
                "can_edit_note": bool(
                    settlement
                    and settlement.settlement_submitted_at
                    and not reconciliation["is_provisional"]
                    and reconciliation["status"] in {"surplus", "shortage"}
                ),
                "can_clear_note": bool(
                    settlement
                    and settlement.reconciliation_note
                    and not reconciliation["is_provisional"]
                    and reconciliation["status"] == "matched"
                ),
            }
        )
    elif settlement:
        base.update(
            {
                "id": settlement.id,
                "settlement_submitted_at": iso_utc(settlement.settlement_submitted_at),
                "created_by": user_summary(settlement.created_by),
                "cashier_updated_by": user_summary(settlement.cashier_updated_by),
                "cashier_updated_at": iso_utc(settlement.cashier_updated_at),
                "admin_updated_by": user_summary(settlement.admin_updated_by),
                "admin_updated_at": iso_utc(settlement.admin_updated_at),
            }
        )
    return base


def branch_day_payload(
    db: Session, branch_id: int, work_date: date, *, cashier: bool
) -> dict[str, Any]:
    branch = db.get(Branch, branch_id)
    if branch is None:
        raise BusinessError(404, "BRANCH_NOT_FOUND", "Branch not found.")
    assignments = required_assignments(db, branch_id, work_date)
    employee_ids = [item.employee_id for item in assignments]
    employees = (
        list(
            db.scalars(
                select(Employee).where(Employee.id.in_(employee_ids)).order_by(Employee.name)
            )
        )
        if employee_ids
        else []
    )
    sales = list(
        db.scalars(
            select(DailySale).where(
                DailySale.branch_id == branch_id, DailySale.work_date == work_date
            )
        )
    )
    sale_by_employee = {sale.employee_id: sale for sale in sales}
    settlement = get_settlement(db, branch_id, work_date)
    completion = completion_view(db, branch_id, work_date)
    closure = closure_at(db, branch_id, work_date)
    service_count = sum(sale.service_count for sale in sales)
    employees_sales_total = (
        settlement.employees_sales_total
        if settlement
        else sum((sale.sales_total for sale in sales), ZERO)
    )
    employee_rows = []
    for employee in [] if closure else employees:
        sale = sale_by_employee.get(employee.id)
        if cashier:
            admin_locked = bool(sale and sale.admin_locked_at)
            cashier_can_edit = work_date == riyadh_today() and not admin_locked
            employee_rows.append(
                {
                    "employee": employee_summary(db, employee, work_date),
                    "record_status": (
                        "admin_locked" if admin_locked else "saved" if sale else "missing"
                    ),
                    "sale": sale_view(db, sale, cashier=True) if sale else None,
                    "can_edit": cashier_can_edit,
                    "lock_message": "تم التعديل بواسطة الإدارة" if admin_locked else None,
                }
            )
        else:
            employee_rows.append(
                {
                    "employee": employee_summary(db, employee, work_date),
                    "required_for_day": True,
                    "record_status": "saved" if sale else "missing",
                    "sale": sale_view(db, sale, cashier=False) if sale else None,
                }
            )
    payload: dict[str, Any] = {
        "branch": branch_summary(db, branch, work_date),
        "work_date": work_date.isoformat(),
        "closure": (
            {"id": closure.id, "status": closure.status.value, "reason": closure.close_reason}
            if closure
            else None
        ),
        "completion": completion,
        "totals": {
            "service_count": service_count,
            "employees_sales_total": money(employees_sales_total),
        },
        "employees": employee_rows,
        "settlement": (
            None
            if closure or settlement is None
            else settlement_view(db, settlement, branch_id, work_date, cashier=cashier)
        ),
    }
    if cashier:
        payload["timezone"] = "Asia/Riyadh"
    return payload


def reconciliation_warning(view: dict[str, Any]) -> dict[str, str] | None:
    if view["status"] not in {"surplus", "shortage"}:
        return None
    return {
        "code": "RECONCILIATION_DIFFERENCE",
        "message": f"Saved with {view['status']} of {view['difference_amount']} SAR.",
    }


def status_matches_filter(
    reconciliation: dict[str, Any],
    value: Literal["matched", "surplus", "shortage", "provisional"] | None,
) -> bool:
    if value is None:
        return True
    if value == "provisional":
        return bool(reconciliation["is_provisional"])
    return not reconciliation["is_provisional"] and reconciliation["status"] == value


def close_open_period(previous: BranchStatusPeriod | EmployeeBranchAssignment, start: date) -> None:
    previous.effective_to = start - timedelta(days=1)
