from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app.core.security import hash_pin
from app.core.time import riyadh_today, utc_now
from app.db.session import SessionLocal
from app.models.entities import (
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
    User,
    UserRole,
)


def seed() -> None:
    with SessionLocal() as db:
        if db.scalar(select(func.count(User.id))):
            print("Seed skipped: users already exist.")
            return

        today = riyadh_today()
        now = utc_now()
        branches = [
            Branch(name=name)
            for name in [
                "فرع العليا",
                "فرع الروضة",
                "فرع الياسمين",
                "فرع قرطبة",
                "فرع الملقا",
                "فرع الصحافة",
            ]
        ]
        db.add_all(branches)
        db.flush()

        primary = User(
            display_name="مديرة النظام",
            phone="0500000001",
            pin_hash=hash_pin("123456"),
            role=UserRole.ADMIN,
            is_primary_admin=True,
            status=EntityStatus.ACTIVE,
            preferred_language=Language.AR,
        )
        admin = User(
            display_name="مديرة التشغيل",
            phone="0500000002",
            pin_hash=hash_pin("123456"),
            role=UserRole.ADMIN,
            is_primary_admin=False,
            status=EntityStatus.ACTIVE,
            preferred_language=Language.AR,
        )
        db.add_all([primary, admin])
        db.flush()

        for branch in branches:
            db.add(
                BranchStatusPeriod(
                    branch_id=branch.id,
                    status=EntityStatus.ACTIVE,
                    effective_from=today - timedelta(days=365),
                    created_by_user_id=primary.id,
                )
            )

        cashiers = []
        for index, branch in enumerate(branches, start=1):
            cashier = User(
                display_name=f"كاشيرة {branch.name.replace('فرع ', '')}",
                phone=f"05000001{index:02d}",
                pin_hash=hash_pin("123456"),
                role=UserRole.CASHIER,
                is_primary_admin=False,
                status=EntityStatus.ACTIVE,
                preferred_language=Language.AR,
                branch_id=branch.id,
            )
            cashiers.append(cashier)
        db.add_all(cashiers)
        db.flush()

        employee_names = [
            "نوف",
            "سارة",
            "ريم",
            "لينا",
            "جود",
            "هند",
        ]
        employees_by_branch: dict[int, list[Employee]] = {}
        code = 1
        for branch in branches:
            employees_by_branch[branch.id] = []
            for name in employee_names:
                employee = Employee(
                    employee_code=f"E{code:04d}", name=f"{name} {branch.name.replace('فرع ', '')}"
                )
                code += 1
                db.add(employee)
                db.flush()
                db.add(
                    EmployeeBranchAssignment(
                        employee_id=employee.id,
                        branch_id=branch.id,
                        assignment_status=EntityStatus.ACTIVE,
                        effective_from=today - timedelta(days=365),
                        created_by_user_id=primary.id,
                    )
                )
                employees_by_branch[branch.id].append(employee)

        for day_offset in range(6, -1, -1):
            work_date = today - timedelta(days=day_offset)
            for branch_index, branch in enumerate(branches):
                if work_date == today and branch_index == 5:
                    continue
                employees = employees_by_branch[branch.id]
                included = employees
                if work_date == today and branch_index == 1:
                    included = employees[:-2]
                employee_total = Decimal("0.00")
                for employee_index, employee in enumerate(included):
                    sales = Decimal(
                        4200 + branch_index * 240 + employee_index * 315 + day_offset * 25
                    )
                    services = 18 + employee_index * 3 + branch_index
                    employee_total += sales
                    db.add(
                        DailySale(
                            branch_id=branch.id,
                            employee_id=employee.id,
                            work_date=work_date,
                            service_count=services,
                            sales_total=sales,
                            created_by_user_id=cashiers[branch_index].id,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                settlement = BranchDailySettlement(
                    branch_id=branch.id,
                    work_date=work_date,
                    employees_sales_total=employee_total,
                    employees_total_updated_at=now,
                    row_created_at=now,
                )
                if not (work_date == today and branch_index == 4):
                    difference = Decimal("0.00")
                    if branch_index == 2:
                        difference = Decimal("120.00")
                    elif branch_index == 3:
                        difference = Decimal("-180.00")
                    income = employee_total + difference
                    cash = (income * Decimal("0.55")).quantize(Decimal("0.01"))
                    settlement.cash_total = cash
                    settlement.bank_total = income - cash
                    settlement.settlement_submitted_at = now
                    settlement.created_by_user_id = cashiers[branch_index].id
                    if difference:
                        settlement.reconciliation_note = "تمت مراجعة فرق التسوية مع الفرع."
                db.add(settlement)

        db.add(
            BranchClosureDay(
                branch_id=branches[5].id,
                work_date=today,
                status=ClosureStatus.CLOSED,
                close_reason="إغلاق مجدول للصيانة",
                closed_by_user_id=primary.id,
                closed_at=now,
            )
        )
        db.commit()
        print("Seed complete.")
        print("Primary admin: 0500000001 / 123456")
        print("Cashier example: 0500000101 / 123456")


if __name__ == "__main__":
    seed()
