"""Typed row validation, one pydantic model per CSV. SOURCE-SPECIFIC parsing lives here
(and in datasets.py) so the detector never sees raw CSV quirks.

Key choices:
* AwareDatetime  -> a timestamp without UTC offset is REJECTED (no silent timezone guess).
* Decimal        -> quantities never go through float.
* Literal enums  -> an unknown status is rejected visibly instead of being guessed.
"""
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


def _blank_to_none(v):
    return None if isinstance(v, str) and v.strip() == "" else v


class _Row(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")


class ProductAliasRow(_Row):
    source_system: Literal["ERP", "WMS"]
    external_item_id: str = Field(min_length=1)
    canonical_product_id: str = Field(min_length=1)
    source_uom: str = Field(min_length=1)
    base_uom: str = Field(min_length=1)
    to_base_factor: Decimal = Field(gt=0)

    @field_validator("source_uom", "base_uom")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()


class LocationAliasRow(_Row):
    source_system: Literal["ERP", "WMS", "HRMS"]
    external_location_id: str = Field(min_length=1)
    canonical_location_id: str = Field(min_length=1)
    region_id: str = Field(min_length=1)


class PurchaseOrderLineRow(_Row):
    source_record_id: str = Field(min_length=1)
    po_number: str = Field(min_length=1)
    line_number: int = Field(gt=0)
    supplier_code: str = Field(min_length=1)
    location_code: str = Field(min_length=1)
    item_code: str = Field(min_length=1)
    ordered_qty: Decimal = Field(gt=0)
    ordered_uom: str = Field(min_length=1)
    status: Literal["OPEN", "CANCELLED", "CLOSED"]
    expected_receipt_by: AwareDatetime

    @field_validator("ordered_uom")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @field_validator("status", mode="before")
    @classmethod
    def _status_upper(cls, v):
        return v.strip().upper() if isinstance(v, str) else v


class GoodsReceiptLineRow(_Row):
    source_record_id: str = Field(min_length=1)
    receipt_number: str = Field(min_length=1)
    receipt_line: int = Field(gt=0)
    po_number: str = Field(min_length=1)
    po_line_number: int = Field(gt=0)
    location_code: str = Field(min_length=1)
    product_ref: str = Field(min_length=1)
    accepted_qty: Decimal = Field(ge=0)
    uom: str = Field(min_length=1)
    received_at: AwareDatetime
    posting_status: Literal["POSTED", "PENDING", "REVERSED"]

    @field_validator("uom")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @field_validator("posting_status", mode="before")
    @classmethod
    def _status_upper(cls, v):
        return v.strip().upper() if isinstance(v, str) else v


class EmployeeRow(_Row):
    source_employee_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    employment_status: Literal["ACTIVE", "INACTIVE"]
    role: str = Field(min_length=1)
    location_code: str | None = None       # optional: regional roles have no store
    manager_employee_id: str | None = None
    region_id: str | None = None

    _blank = field_validator("location_code", "manager_employee_id", "region_id", mode="before")(_blank_to_none)

    @model_validator(mode="after")
    def _needs_scope(self):
        if not self.location_code and not self.region_id:
            raise ValueError("employee has neither a location nor a region scope")
        return self


class WorkforceStatusRow(_Row):
    source_employee_id: str = Field(min_length=1)
    availability_status: Literal["ON_SHIFT", "OFF_SHIFT"]
    shift_start: AwareDatetime
    shift_end: AwareDatetime
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def _shift_order(self):
        if self.shift_end <= self.shift_start:
            raise ValueError("shift_end must be after shift_start")
        return self
