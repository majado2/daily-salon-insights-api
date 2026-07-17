from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from app.api.deps import CashierUser, DbSession, envelope, require_csrf
from app.core.errors import BusinessError
from app.core.time import riyadh_today
from app.schemas.requests import NoteRequest, SaleUpsertRequest, SettlementUpsertRequest
from app.services.business import (
    branch_day_payload,
    reconciliation_warning,
    settlement_view,
    update_settlement_note,
    upsert_sale,
    upsert_settlement,
)

router = APIRouter(prefix="/cashier", tags=["Cashier day"])
CashierWorkDate = Annotated[date, Header(alias="X-Work-Date")]


@router.get("/today")
def today(
    request: Request,
    user: CashierUser,
    db: DbSession,
    work_date: date | None = None,
) -> dict[str, object]:
    if user.branch_id is None:
        raise BusinessError(403, "CASHIER_BRANCH_REQUIRED", "The cashier has no branch.")
    current_date = riyadh_today()
    selected_date = work_date or current_date
    if selected_date > current_date:
        raise BusinessError(
            422,
            "FUTURE_WORK_DATE_NOT_ALLOWED",
            "The cashier cannot view a future work date.",
        )
    return envelope(
        request,
        branch_day_payload(db, user.branch_id, selected_date, cashier=True),
    )


@router.put(
    "/today/employees/{employee_id}/sale",
    dependencies=[Depends(require_csrf)],
)
def save_employee_sale(
    employee_id: int,
    payload: SaleUpsertRequest,
    request: Request,
    user: CashierUser,
    db: DbSession,
    work_date: CashierWorkDate,
) -> dict[str, object]:
    if user.branch_id is None:
        raise BusinessError(403, "CASHIER_BRANCH_REQUIRED", "The cashier has no branch.")
    _, settlement = upsert_sale(
        db,
        actor=user,
        branch_id=user.branch_id,
        employee_id=employee_id,
        work_date=work_date,
        service_count=payload.service_count,
        sales_total=payload.sales_total,
        confirm_zero_mismatch=payload.confirm_zero_mismatch,
    )
    day = branch_day_payload(db, user.branch_id, work_date, cashier=True)
    warning = reconciliation_warning(
        settlement_view(db, settlement, user.branch_id, work_date, cashier=True)["reconciliation"]
    )
    employee_record = next(
        item for item in day["employees"] if item["employee"]["id"] == employee_id
    )
    response = envelope(
        request,
        {
            "employee_record": employee_record,
            "completion": day["completion"],
            "totals": day["totals"],
            "settlement": day["settlement"],
        },
    )
    response["warnings"] = [warning] if warning else []
    return response


@router.put("/today/settlement", dependencies=[Depends(require_csrf)])
def save_settlement(
    payload: SettlementUpsertRequest,
    request: Request,
    user: CashierUser,
    db: DbSession,
    work_date: CashierWorkDate,
) -> dict[str, object]:
    if user.branch_id is None:
        raise BusinessError(403, "CASHIER_BRANCH_REQUIRED", "The cashier has no branch.")
    settlement = upsert_settlement(
        db,
        actor=user,
        branch_id=user.branch_id,
        work_date=work_date,
        cash_total=payload.cash_total,
        bank_total=payload.bank_total,
    )
    view = settlement_view(db, settlement, user.branch_id, work_date, cashier=True)
    warning = reconciliation_warning(view["reconciliation"])
    response = envelope(
        request,
        {
            "settlement": view,
            "completion": branch_day_payload(db, user.branch_id, work_date, cashier=True)[
                "completion"
            ],
        },
    )
    response["warnings"] = [warning] if warning else []
    return response


@router.patch("/today/settlement/note", dependencies=[Depends(require_csrf)])
def save_settlement_note(
    payload: NoteRequest,
    request: Request,
    user: CashierUser,
    db: DbSession,
    work_date: CashierWorkDate,
) -> dict[str, object]:
    if user.branch_id is None:
        raise BusinessError(403, "CASHIER_BRANCH_REQUIRED", "The cashier has no branch.")
    update_settlement_note(
        db,
        actor=user,
        branch_id=user.branch_id,
        work_date=work_date,
        note=payload.reconciliation_note,
    )
    day = branch_day_payload(db, user.branch_id, work_date, cashier=True)
    return envelope(
        request,
        {"settlement": day["settlement"], "completion": day["completion"]},
    )
