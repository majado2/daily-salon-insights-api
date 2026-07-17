from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import BranchDailySettlement, DailySale, Employee
from app.services.business import (
    branch_summary,
    completion_view,
    employee_summary,
    money,
    reconciliation_view,
    status_matches_filter,
    user_summary,
)

ReconciliationFilter = Literal["matched", "surplus", "shortage", "provisional"]


def settlement_rows(
    db: Session,
    *,
    date_from: date,
    date_to: date,
    branch_id: int | None = None,
    employee_id: int | None = None,
    reconciliation_filter: ReconciliationFilter | None = None,
) -> list[dict[str, Any]]:
    statement = (
        select(BranchDailySettlement)
        .where(
            BranchDailySettlement.work_date >= date_from,
            BranchDailySettlement.work_date <= date_to,
        )
        .order_by(BranchDailySettlement.work_date.desc(), BranchDailySettlement.branch_id)
    )
    if branch_id:
        statement = statement.where(BranchDailySettlement.branch_id == branch_id)
    settlements = list(db.scalars(statement))
    rows: list[dict[str, Any]] = []
    for settlement in settlements:
        if employee_id:
            has_employee = db.scalar(
                select(DailySale.id)
                .where(
                    DailySale.employee_id == employee_id,
                    DailySale.branch_id == settlement.branch_id,
                    DailySale.work_date == settlement.work_date,
                )
                .limit(1)
            )
            if not has_employee:
                continue
        reconciliation = reconciliation_view(
            db, settlement.branch_id, settlement.work_date, settlement
        )
        if not status_matches_filter(reconciliation, reconciliation_filter):
            continue
        sales = list(
            db.scalars(
                select(DailySale).where(
                    DailySale.branch_id == settlement.branch_id,
                    DailySale.work_date == settlement.work_date,
                )
            )
        )
        rows.append(
            {
                "id": settlement.id,
                "branch": branch_summary(db, settlement.branch, settlement.work_date),
                "work_date": settlement.work_date.isoformat(),
                "completion": completion_view(db, settlement.branch_id, settlement.work_date),
                "service_count": sum(item.service_count for item in sales),
                "cash_total": money(settlement.cash_total),
                "bank_total": money(settlement.bank_total),
                "branch_income_total": money(settlement.branch_income_total),
                "employees_sales_total": money(settlement.employees_sales_total),
                "reconciliation": reconciliation,
                "reconciliation_note": settlement.reconciliation_note,
                "created_by": user_summary(settlement.created_by),
                "cashier_updated_by": user_summary(settlement.cashier_updated_by),
                "cashier_updated_at": settlement.cashier_updated_at,
                "admin_updated_by": user_summary(settlement.admin_updated_by),
                "admin_updated_at": settlement.admin_updated_at,
            }
        )
    return rows


def employee_sale_rows(
    db: Session,
    *,
    date_from: date,
    date_to: date,
    branch_id: int | None = None,
    employee_id: int | None = None,
    reconciliation_filter: ReconciliationFilter | None = None,
) -> list[dict[str, Any]]:
    statement = (
        select(DailySale)
        .where(DailySale.work_date >= date_from, DailySale.work_date <= date_to)
        .order_by(DailySale.work_date.desc(), DailySale.branch_id, DailySale.employee_id)
    )
    if branch_id:
        statement = statement.where(DailySale.branch_id == branch_id)
    if employee_id:
        statement = statement.where(DailySale.employee_id == employee_id)
    rows: list[dict[str, Any]] = []
    for sale in db.scalars(statement):
        if reconciliation_filter:
            reconciliation = reconciliation_view(db, sale.branch_id, sale.work_date)
            if not status_matches_filter(reconciliation, reconciliation_filter):
                continue
        rows.append(
            {
                "id": sale.id,
                "branch": branch_summary(db, sale.branch, sale.work_date),
                "employee": employee_summary(db, sale.employee, sale.work_date),
                "work_date": sale.work_date.isoformat(),
                "service_count": sale.service_count,
                "sales_total": money(sale.sales_total),
                "created_by": user_summary(sale.created_by),
                "cashier_updated_by": user_summary(sale.cashier_updated_by),
                "admin_updated_by": user_summary(sale.admin_updated_by),
            }
        )
    return rows


def report_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    service_count = 0
    employees_sales_total = Decimal("0.00")
    cash_total = Decimal("0.00")
    bank_total = Decimal("0.00")
    branch_income_total = Decimal("0.00")
    difference_amount = Decimal("0.00")
    for row in rows:
        service_count += int(row["service_count"])
        employees_sales_total += Decimal(row["employees_sales_total"] or "0.00")
        cash_total += Decimal(row["cash_total"] or "0.00")
        bank_total += Decimal(row["bank_total"] or "0.00")
        branch_income_total += Decimal(row["branch_income_total"] or "0.00")
        difference_amount += Decimal(row["reconciliation"]["difference_amount"] or "0.00")
    return {
        "service_count": service_count,
        "employees_sales_total": money(employees_sales_total),
        "cash_total": money(cash_total),
        "bank_total": money(bank_total),
        "branch_income_total": money(branch_income_total),
        "difference_amount": money(difference_amount),
    }


def aggregate_rows(
    rows: list[dict[str, Any]], group_by: Literal["day", "month", "year", "branch"]
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        work_date = date.fromisoformat(row["work_date"])
        if group_by == "day":
            key = work_date.isoformat()
        elif group_by == "month":
            key = work_date.strftime("%Y-%m")
        elif group_by == "year":
            key = str(work_date.year)
        else:
            key = str(row["branch"]["id"])
        groups[key].append(row)
    output = []
    for key, items in groups.items():
        summary = report_summary(items)
        counts = {
            "matched_days": 0,
            "surplus_days": 0,
            "shortage_days": 0,
            "provisional_days": 0,
            "incomplete_days": 0,
        }
        for item in items:
            reconciliation = item["reconciliation"]
            if reconciliation["is_provisional"]:
                counts["provisional_days"] += 1
            else:
                status_key = f"{reconciliation['status']}_days"
                if status_key in counts:
                    counts[status_key] += 1
            if item["completion"]["status"] == "incomplete":
                counts["incomplete_days"] += 1
        output.append(
            {
                "group": key,
                "branch": items[0]["branch"] if group_by == "branch" else None,
                **summary,
                **counts,
            }
        )
    return sorted(output, key=lambda item: item["group"])


def ranking_rows(
    db: Session,
    *,
    entity: Literal["employee", "branch"],
    date_from: date,
    date_to: date,
    branch_id: int | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    sales = list(
        db.scalars(
            select(DailySale).where(
                DailySale.work_date >= date_from,
                DailySale.work_date <= date_to,
                *([DailySale.branch_id == branch_id] if branch_id else []),
            )
        )
    )
    grouped: dict[int, dict[str, Any]] = {}
    for sale in sales:
        entity_id = sale.employee_id if entity == "employee" else sale.branch_id
        if entity_id not in grouped:
            if entity == "employee":
                employee = db.get(Employee, entity_id)
                name = employee.name if employee else str(entity_id)
                branch = branch_summary(db, sale.branch, sale.work_date)
            else:
                name = sale.branch.name
                branch = None
            grouped[entity_id] = {
                "id": entity_id,
                "name": name,
                "branch": branch,
                "service_count": 0,
                "sales_total": Decimal("0.00"),
            }
        grouped[entity_id]["service_count"] += sale.service_count
        grouped[entity_id]["sales_total"] += sale.sales_total
    ranked = sorted(
        grouped.values(),
        key=lambda item: (-item["sales_total"], item["name"].casefold(), item["id"]),
    )[:limit]
    return [
        {
            "rank": index,
            "entity": entity,
            "id": item["id"],
            "name": item["name"],
            "branch": item["branch"],
            "service_count": item["service_count"],
            "sales_total": money(item["sales_total"]),
        }
        for index, item in enumerate(ranked, start=1)
    ]
