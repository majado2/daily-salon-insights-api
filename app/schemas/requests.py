from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LoginRequest(StrictModel):
    phone: str = Field(pattern=r"^05\d{8}$")
    pin: str = Field(pattern=r"^\d{6}$")


class PreferenceRequest(StrictModel):
    preferred_language: Literal["ar", "en"]


class PinChangeRequest(StrictModel):
    current_pin: str = Field(pattern=r"^\d{6}$")
    new_pin: str = Field(pattern=r"^\d{6}$")


class SaleUpsertRequest(StrictModel):
    service_count: int = Field(ge=0, le=1_000_000)
    sales_total: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    confirm_zero_mismatch: bool = False

    @field_validator("sales_total")
    @classmethod
    def normalize_money(cls, value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"))


class AdminSaleUpsertRequest(SaleUpsertRequest):
    reason: str = Field(min_length=1, max_length=500)


class SettlementUpsertRequest(StrictModel):
    cash_total: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    bank_total: Decimal = Field(ge=0, max_digits=14, decimal_places=2)

    @field_validator("cash_total", "bank_total")
    @classmethod
    def normalize_money(cls, value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"))


class AdminSettlementUpsertRequest(SettlementUpsertRequest):
    reconciliation_note: str | None = Field(default=None, max_length=500)
    reason: str = Field(min_length=1, max_length=500)


class NoteRequest(StrictModel):
    reconciliation_note: str | None = Field(default=None, max_length=500)


class AdminNoteRequest(NoteRequest):
    reason: str = Field(min_length=1, max_length=500)


class BranchCreateRequest(StrictModel):
    name: str = Field(min_length=2, max_length=150)
    effective_date: date


class NameUpdateRequest(StrictModel):
    name: str = Field(min_length=2, max_length=150)


class BranchStatusRequest(StrictModel):
    status: Literal["active", "disabled"]
    effective_date: date
    note: str | None = Field(default=None, max_length=500)


class EmployeeCreateRequest(StrictModel):
    name: str = Field(min_length=2, max_length=150)
    branch_id: int
    effective_date: date


class EmployeeTransferRequest(StrictModel):
    target_branch_id: int
    effective_date: date


class EmployeeDisableRequest(StrictModel):
    effective_date: date


class EmployeeReactivateRequest(StrictModel):
    branch_id: int
    effective_date: date


class AccountCreateRequest(StrictModel):
    display_name: str = Field(min_length=2, max_length=150)
    phone: str = Field(pattern=r"^05\d{8}$")
    pin: str = Field(pattern=r"^\d{6}$")
    branch_id: int | None = None
    preferred_language: Literal["ar", "en"] = "ar"


class AccountUpdateRequest(StrictModel):
    display_name: str = Field(min_length=2, max_length=150)
    phone: str = Field(pattern=r"^05\d{8}$")
    preferred_language: Literal["ar", "en"] = "ar"


class AccountTransferRequest(StrictModel):
    target_branch_id: int


class AccountReactivateRequest(StrictModel):
    branch_id: int | None = None


class PinResetRequest(StrictModel):
    new_pin: str = Field(pattern=r"^\d{6}$")


class ClosureCreateRequest(StrictModel):
    branch_id: int
    work_date: date
    reason: str = Field(min_length=1, max_length=500)


class ReasonRequest(StrictModel):
    reason: str = Field(min_length=1, max_length=500)
