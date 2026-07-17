from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.api.deps import AdminUser, DbSession, envelope, require_csrf
from app.core.time import iso_utc, riyadh_today, utc_now
from app.models.entities import Branch, EntityStatus
from app.schemas.requests import (
    AdminNoteRequest,
    AdminSaleUpsertRequest,
    AdminSettlementUpsertRequest,
)
from app.services.business import (
    branch_day_payload,
    branch_status_at,
    completion_view,
    money,
    reconciliation_view,
    reconciliation_warning,
    settlement_view,
    update_settlement_note,
    upsert_sale,
    upsert_settlement,
)
from app.services.reports import ranking_rows

router = APIRouter(prefix="/admin", tags=["Administration days"])


@router.get("/dashboard")
def dashboard(request: Request, user: AdminUser, db: DbSession) -> dict[str, object]:
    del user
    work_date = riyadh_today()
    branches = list(db.scalars(select(Branch).order_by(Branch.name)))
    rows = []
    employee_sales_total = Decimal("0.00")
    cash_total = Decimal("0.00")
    bank_total = Decimal("0.00")
    branch_income_total = Decimal("0.00")
    difference_amount = Decimal("0.00")
    total_service_count = 0
    incomplete_branches = 0
    for branch in branches:
        status = branch_status_at(db, branch.id, work_date)
        day = branch_day_payload(db, branch.id, work_date, cashier=False)
        completion = day["completion"]
        if status != EntityStatus.ACTIVE and completion["status"] != "closed":
            continue
        settlement = day["settlement"]
        service_count = int(day["totals"]["service_count"])
        branch_view = dict(day["branch"])
        if completion["status"] == "closed":
            branch_view["status"] = "closed"
        reconciliation = (
            settlement["reconciliation"]
            if settlement
            else reconciliation_view(db, branch.id, work_date)
        )
        settlement_submitted = bool(settlement and settlement["settlement_submitted_at"])
        row = {
            "branch": branch_view,
            "completion": completion,
            "service_count": service_count,
            "employees_sales_total": day["totals"]["employees_sales_total"],
            "cash_total": settlement["cash_total"] if settlement else None,
            "bank_total": settlement["bank_total"] if settlement else None,
            "branch_income_total": settlement["branch_income_total"] if settlement else None,
            "reconciliation": reconciliation,
            "has_reconciliation_note": bool(settlement and settlement["reconciliation_note"]),
            "settlement_submitted": settlement_submitted,
        }
        rows.append(row)
        if completion["status"] != "closed":
            employee_sales_total += Decimal(day["totals"]["employees_sales_total"] or "0")
            cash_total += Decimal(settlement["cash_total"] or "0") if settlement else Decimal(0)
            bank_total += Decimal(settlement["bank_total"] or "0") if settlement else Decimal(0)
            branch_income_total += (
                Decimal(settlement["branch_income_total"] or "0") if settlement else Decimal(0)
            )
            if settlement_submitted and reconciliation["difference_amount"] is not None:
                difference_amount += Decimal(reconciliation["difference_amount"])
            total_service_count += service_count
            if completion["status"] == "incomplete":
                incomplete_branches += 1

    return envelope(
        request,
        {
            "work_date": work_date.isoformat(),
            "generated_at": iso_utc(utc_now()),
            "totals": {
                "employees_sales_total": money(employee_sales_total),
                "cash_total": money(cash_total),
                "bank_total": money(bank_total),
                "branch_income_total": money(branch_income_total),
                "difference_amount": money(difference_amount),
                "service_count": total_service_count,
                "incomplete_branches": incomplete_branches,
            },
            "branches": rows,
            "top_employees": ranking_rows(
                db,
                entity="employee",
                date_from=work_date,
                date_to=work_date,
                limit=10,
            ),
            "top_branches": ranking_rows(
                db,
                entity="branch",
                date_from=work_date,
                date_to=work_date,
                limit=10,
            ),
        },
    )


@router.get("/branches/{branch_id}/days/{work_date}")
def branch_day(
    branch_id: int,
    work_date: date,
    request: Request,
    user: AdminUser,
    db: DbSession,
) -> dict[str, object]:
    del user
    return envelope(request, branch_day_payload(db, branch_id, work_date, cashier=False))


@router.put(
    "/branches/{branch_id}/days/{work_date}/employees/{employee_id}/sale",
    dependencies=[Depends(require_csrf)],
)
def admin_save_sale(
    branch_id: int,
    work_date: date,
    employee_id: int,
    payload: AdminSaleUpsertRequest,
    request: Request,
    user: AdminUser,
    db: DbSession,
) -> dict[str, object]:
    upsert_sale(
        db,
        actor=user,
        branch_id=branch_id,
        employee_id=employee_id,
        work_date=work_date,
        service_count=payload.service_count,
        sales_total=payload.sales_total,
        confirm_zero_mismatch=payload.confirm_zero_mismatch,
        admin_reason=payload.reason,
    )
    return envelope(request, branch_day_payload(db, branch_id, work_date, cashier=False))


@router.put(
    "/branches/{branch_id}/days/{work_date}/settlement",
    dependencies=[Depends(require_csrf)],
)
def admin_save_settlement(
    branch_id: int,
    work_date: date,
    payload: AdminSettlementUpsertRequest,
    request: Request,
    user: AdminUser,
    db: DbSession,
) -> dict[str, object]:
    settlement = upsert_settlement(
        db,
        actor=user,
        branch_id=branch_id,
        work_date=work_date,
        cash_total=payload.cash_total,
        bank_total=payload.bank_total,
        reconciliation_note=payload.reconciliation_note,
        admin_reason=payload.reason,
    )
    reconciliation = reconciliation_view(db, branch_id, work_date, settlement)
    response = envelope(
        request,
        {
            "settlement": settlement_view(db, settlement, branch_id, work_date, cashier=False),
            "completion": completion_view(db, branch_id, work_date),
            "reconciliation": reconciliation,
        },
    )
    warning = reconciliation_warning(reconciliation)
    response["warnings"] = [warning] if warning else []
    return response


@router.patch(
    "/branches/{branch_id}/days/{work_date}/settlement/note",
    dependencies=[Depends(require_csrf)],
)
def admin_save_note(
    branch_id: int,
    work_date: date,
    payload: AdminNoteRequest,
    request: Request,
    user: AdminUser,
    db: DbSession,
) -> dict[str, object]:
    settlement = update_settlement_note(
        db,
        actor=user,
        branch_id=branch_id,
        work_date=work_date,
        note=payload.reconciliation_note,
        admin_reason=payload.reason,
    )
    return envelope(request, settlement_view(db, settlement, branch_id, work_date, cashier=False))
