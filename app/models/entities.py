from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CHAR,
    JSON,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


ID_TYPE = mysql.BIGINT(unsigned=True).with_variant(Integer, "sqlite")
UNSIGNED_INT_TYPE = mysql.INTEGER(unsigned=True).with_variant(Integer, "sqlite")
UNSIGNED_TINYINT_TYPE = mysql.TINYINT(unsigned=True).with_variant(Integer, "sqlite")
DATETIME_TYPE = mysql.DATETIME(fsp=6).with_variant(DateTime(), "sqlite")


class EntityStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class UserRole(StrEnum):
    ADMIN = "admin"
    CASHIER = "cashier"


class Language(StrEnum):
    AR = "ar"
    EN = "en"


class ClosureStatus(StrEnum):
    CLOSED = "closed"
    REOPENED = "reopened"


def enum_column(enum_class: type[StrEnum], *, length: int) -> Enum:
    return Enum(
        enum_class,
        values_callable=lambda members: [item.value for item in members],
        native_enum=False,
        length=length,
    )


class Branch(Base):
    __tablename__ = "branches"
    __table_args__ = (Index("ix_branches_name", "name"),)

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME_TYPE, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DATETIME_TYPE, default=utcnow, onupdate=utcnow, nullable=False
    )

    status_periods: Mapped[list[BranchStatusPeriod]] = relationship(
        back_populates="branch", cascade="all, delete-orphan"
    )


class BranchStatusPeriod(Base):
    __tablename__ = "branch_status_periods"
    __table_args__ = (
        UniqueConstraint("branch_id", "effective_from", name="uq_branch_status_start"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_branch_status_dates",
        ),
        Index("ix_branch_status_lookup", "branch_id", "effective_from", "effective_to"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    branch_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[EntityStatus] = mapped_column(
        enum_column(EntityStatus, length=20), nullable=False
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    created_by_user_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DATETIME_TYPE, default=utcnow, nullable=False)

    branch: Mapped[Branch] = relationship(back_populates="status_periods")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("phone", name="uq_users_phone"),
        UniqueConstraint("active_cashier_branch_id", name="uq_active_cashier_branch"),
        UniqueConstraint("primary_admin_guard", name="uq_primary_admin_guard"),
        CheckConstraint(
            "(role = 'cashier' AND branch_id IS NOT NULL AND is_primary_admin = 0) "
            "OR (role = 'admin' AND branch_id IS NULL)",
            name="ck_user_role_branch",
        ),
        CheckConstraint("phone REGEXP '^05[0-9]{8}$'", name="ck_users_phone_format").ddl_if(
            dialect="mysql"
        ),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    display_name: Mapped[str] = mapped_column(String(150), nullable=False)
    phone: Mapped[str] = mapped_column(CHAR(10), nullable=False)
    pin_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(enum_column(UserRole, length=20), nullable=False)
    is_primary_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[EntityStatus] = mapped_column(
        enum_column(EntityStatus, length=20), default=EntityStatus.ACTIVE, nullable=False
    )
    preferred_language: Mapped[Language] = mapped_column(
        enum_column(Language, length=5), default=Language.AR, nullable=False
    )
    branch_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("branches.id", ondelete="RESTRICT")
    )
    failed_login_attempts: Mapped[int] = mapped_column(
        UNSIGNED_TINYINT_TYPE, default=0, nullable=False
    )
    locked_until: Mapped[datetime | None] = mapped_column(DATETIME_TYPE)
    last_login_at: Mapped[datetime | None] = mapped_column(DATETIME_TYPE)
    created_at: Mapped[datetime] = mapped_column(DATETIME_TYPE, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DATETIME_TYPE, default=utcnow, onupdate=utcnow, nullable=False
    )
    active_cashier_branch_id: Mapped[int | None] = mapped_column(
        ID_TYPE,
        Computed(
            "CASE WHEN role = 'cashier' AND status = 'active' THEN branch_id ELSE NULL END",
            persisted=True,
        ),
    )
    primary_admin_guard: Mapped[int | None] = mapped_column(
        Integer,
        Computed("CASE WHEN is_primary_admin = 1 THEN 1 ELSE NULL END", persisted=True),
    )

    branch: Mapped[Branch | None] = relationship()


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    employee_code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME_TYPE, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DATETIME_TYPE, default=utcnow, onupdate=utcnow, nullable=False
    )

    assignments: Mapped[list[EmployeeBranchAssignment]] = relationship(
        back_populates="employee", cascade="all, delete-orphan"
    )


class EmployeeBranchAssignment(Base):
    __tablename__ = "employee_branch_assignments"
    __table_args__ = (
        UniqueConstraint("employee_id", "effective_from", name="uq_employee_assignment_start"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_employee_assignment_dates",
        ),
        Index(
            "ix_employee_assignment_required",
            "branch_id",
            "assignment_status",
            "effective_from",
            "effective_to",
        ),
        Index(
            "ix_employee_assignment_history",
            "employee_id",
            "effective_from",
            "effective_to",
        ),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False
    )
    branch_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False
    )
    assignment_status: Mapped[EntityStatus] = mapped_column(
        enum_column(EntityStatus, length=20), nullable=False
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    created_by_user_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DATETIME_TYPE, default=utcnow, nullable=False)

    employee: Mapped[Employee] = relationship(back_populates="assignments")
    branch: Mapped[Branch] = relationship()


class DailySale(Base):
    __tablename__ = "daily_sales"
    __table_args__ = (
        UniqueConstraint("employee_id", "work_date", name="uq_daily_sale_employee_date"),
        CheckConstraint("service_count >= 0", name="ck_daily_sale_services_nonnegative"),
        CheckConstraint("sales_total >= 0", name="ck_daily_sale_total_nonnegative"),
        CheckConstraint(
            "(admin_locked_at IS NULL AND admin_locked_by_user_id IS NULL) OR "
            "(admin_locked_at IS NOT NULL AND admin_locked_by_user_id IS NOT NULL)",
            name="ck_daily_sale_admin_lock",
        ),
        Index("ix_daily_sales_branch_date", "branch_id", "work_date"),
        Index("ix_daily_sales_date", "work_date"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    branch_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False
    )
    employee_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False
    )
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    service_count: Mapped[int] = mapped_column(UNSIGNED_INT_TYPE, nullable=False)
    sales_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    cashier_updated_by_user_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="RESTRICT")
    )
    admin_updated_by_user_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="RESTRICT")
    )
    admin_locked_at: Mapped[datetime | None] = mapped_column(DATETIME_TYPE)
    admin_locked_by_user_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DATETIME_TYPE, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DATETIME_TYPE, default=utcnow, onupdate=utcnow, nullable=False
    )

    branch: Mapped[Branch] = relationship()
    employee: Mapped[Employee] = relationship()
    created_by: Mapped[User] = relationship(foreign_keys=[created_by_user_id])
    cashier_updated_by: Mapped[User | None] = relationship(
        foreign_keys=[cashier_updated_by_user_id]
    )
    admin_updated_by: Mapped[User | None] = relationship(foreign_keys=[admin_updated_by_user_id])


class BranchDailySettlement(Base):
    __tablename__ = "branch_daily_settlements"
    __table_args__ = (
        UniqueConstraint("branch_id", "work_date", name="uq_settlement_branch_date"),
        CheckConstraint("cash_total IS NULL OR cash_total >= 0", name="ck_cash_nonnegative"),
        CheckConstraint("bank_total IS NULL OR bank_total >= 0", name="ck_bank_nonnegative"),
        CheckConstraint(
            "(cash_total IS NULL AND bank_total IS NULL AND settlement_submitted_at IS NULL "
            "AND created_by_user_id IS NULL) OR "
            "(cash_total IS NOT NULL AND bank_total IS NOT NULL "
            "AND settlement_submitted_at IS NOT NULL AND created_by_user_id IS NOT NULL)",
            name="ck_settlement_submission",
        ),
        Index("ix_settlements_date", "work_date"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    branch_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False
    )
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    employees_sales_total: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0.00"), nullable=False
    )
    cash_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    bank_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    branch_income_total: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2), Computed("cash_total + bank_total", persisted=True)
    )
    reconciliation_note: Mapped[str | None] = mapped_column(String(500))
    settlement_submitted_at: Mapped[datetime | None] = mapped_column(DATETIME_TYPE)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="RESTRICT")
    )
    cashier_updated_by_user_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="RESTRICT")
    )
    admin_updated_by_user_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="RESTRICT")
    )
    cashier_updated_at: Mapped[datetime | None] = mapped_column(DATETIME_TYPE)
    admin_updated_at: Mapped[datetime | None] = mapped_column(DATETIME_TYPE)
    employees_total_updated_at: Mapped[datetime | None] = mapped_column(DATETIME_TYPE)
    row_created_at: Mapped[datetime] = mapped_column(DATETIME_TYPE, default=utcnow, nullable=False)

    branch: Mapped[Branch] = relationship()
    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_user_id])
    cashier_updated_by: Mapped[User | None] = relationship(
        foreign_keys=[cashier_updated_by_user_id]
    )
    admin_updated_by: Mapped[User | None] = relationship(foreign_keys=[admin_updated_by_user_id])


class BranchClosureDay(Base):
    __tablename__ = "branch_closure_days"
    __table_args__ = (
        UniqueConstraint("branch_id", "work_date", name="uq_branch_closure_date"),
        CheckConstraint(
            "(status = 'closed' AND reopen_reason IS NULL AND reopened_by_user_id IS NULL "
            "AND reopened_at IS NULL) OR "
            "(status = 'reopened' AND reopen_reason IS NOT NULL "
            "AND reopened_by_user_id IS NOT NULL AND reopened_at IS NOT NULL)",
            name="ck_branch_closure_state",
        ),
        Index("ix_branch_closure_date", "work_date", "branch_id"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    branch_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False
    )
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[ClosureStatus] = mapped_column(
        enum_column(ClosureStatus, length=20), nullable=False
    )
    close_reason: Mapped[str] = mapped_column(String(500), nullable=False)
    closed_by_user_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    closed_at: Mapped[datetime] = mapped_column(DATETIME_TYPE, default=utcnow, nullable=False)
    reopen_reason: Mapped[str | None] = mapped_column(String(500))
    reopened_by_user_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="RESTRICT")
    )
    reopened_at: Mapped[datetime | None] = mapped_column(DATETIME_TYPE)

    branch: Mapped[Branch] = relationship()
    closed_by: Mapped[User] = relationship(foreign_keys=[closed_by_user_id])
    reopened_by: Mapped[User | None] = relationship(foreign_keys=[reopened_by_user_id])


class RefreshSession(Base):
    __tablename__ = "refresh_sessions"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_refresh_token_hash"),
        Index(
            "ix_refresh_sessions_user_active",
            "user_id",
            "revoked_at",
            "absolute_expires_at",
        ),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME_TYPE, default=utcnow, nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(
        DATETIME_TYPE, default=utcnow, nullable=False
    )
    absolute_expires_at: Mapped[datetime] = mapped_column(DATETIME_TYPE, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DATETIME_TYPE)

    user: Mapped[User] = relationship()


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_entity", "entity_type", "entity_id", "occurred_at"),
        Index("ix_audit_actor", "actor_user_id", "occurred_at"),
        Index("ix_audit_branch_date", "branch_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="RESTRICT")
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(ID_TYPE)
    branch_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("branches.id", ondelete="RESTRICT")
    )
    old_values: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    new_values: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(String(500))
    occurred_at: Mapped[datetime] = mapped_column(DATETIME_TYPE, default=utcnow, nullable=False)

    actor: Mapped[User | None] = relationship(foreign_keys=[actor_user_id])
    branch: Mapped[Branch | None] = relationship()
