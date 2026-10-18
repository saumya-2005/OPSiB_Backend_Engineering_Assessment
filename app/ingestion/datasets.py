"""Dataset registry: for each CSV, how to validate, map and store it.

Adding another retailer export = add a DatasetSpec (schema + build function).
The generic loader (service.py) and the detector do not change.

Per spec:
  schema         pydantic row model
  model          ORM table
  id_field       raw CSV column used as source_record_id in error reports
  identity_cols  columns of the UNIQUE constraint that makes loading idempotent
  fact_fields    source values that define the row's CONTENT (hashed to detect changes)
  derived_fields values computed by mapping (re-computed if a late alias arrives)
  on_change      what to do if the same identity arrives with different content:
                   "conflict" -> keep the old row, raise a review issue (never overwrite)
                   "update"   -> apply it (only for current-state rosters, see employees)
"""
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from app.config import get_settings
from app.db.models import (
    Employee,
    LocationAlias,
    PoLine,
    ProductAlias,
    ReceiptLine,
    WorkforceSnapshot,
)
from app.mapping.resolver import MappingError, Resolver
from app.ingestion import schemas as s


@dataclass
class Built:
    """Result of mapping one validated row."""
    values: dict[str, Any]                       # source (fact) values + derived values
    issue: tuple[str, str] | None = None         # (reason_code, detail) if mapping failed


@dataclass
class DatasetSpec:
    name: str
    schema: type[BaseModel]
    model: type
    id_field: str
    identity_cols: tuple[str, ...]
    fact_fields: tuple[str, ...]
    build: Callable[[Any, Resolver], Built]
    derived_fields: tuple[str, ...] = ()
    on_change: str = "conflict"
    required_columns: tuple[str, ...] = field(init=False)

    def __post_init__(self):
        self.required_columns = tuple(
            n for n, f in self.schema.model_fields.items() if f.is_required()
        )


_UNMAPPED_DERIVED = dict(canonical_product_id=None, canonical_location_id=None,
                         base_uom=None, mapping_status="UNMAPPED")


def _join_issues(errors: list[MappingError]) -> tuple[str, str] | None:
    if not errors:
        return None
    return errors[0].code, "; ".join(e.detail for e in errors)


# ---------------- reference data ----------------
def _build_product_alias(r: s.ProductAliasRow, _res: Resolver) -> Built:
    return Built(dict(
        source_system=r.source_system, external_item_id=r.external_item_id,
        canonical_product_id=r.canonical_product_id, source_uom=r.source_uom,
        base_uom=r.base_uom, to_base_factor=r.to_base_factor,
    ))


def _build_location_alias(r: s.LocationAliasRow, _res: Resolver) -> Built:
    return Built(dict(
        source_system=r.source_system, external_location_id=r.external_location_id,
        canonical_location_id=r.canonical_location_id, region_id=r.region_id,
    ))


# ---------------- ERP purchase order line ----------------
def _build_po(r: s.PurchaseOrderLineRow, res: Resolver) -> Built:
    values: dict[str, Any] = dict(
        source_system="ERP", source_record_id=r.source_record_id, po_number=r.po_number,
        line_number=r.line_number, supplier_code=r.supplier_code,
        source_location_code=r.location_code, source_item_code=r.item_code,
        ordered_qty=r.ordered_qty, ordered_uom=r.ordered_uom, status=r.status,
        expected_receipt_by=r.expected_receipt_by,
        ordered_qty_base=None, **_UNMAPPED_DERIVED,
    )
    errors: list[MappingError] = []
    loc = prod = None
    try:
        loc = res.location("ERP", r.location_code)
    except MappingError as e:
        errors.append(e)
    try:
        prod = res.to_base("ERP", r.item_code, r.ordered_qty, r.ordered_uom)
    except MappingError as e:
        errors.append(e)
    if not errors:
        values.update(canonical_location_id=loc.canonical_location_id,
                      canonical_product_id=prod[0], base_uom=prod[1],
                      ordered_qty_base=prod[2], mapping_status="MAPPED")
    return Built(values, _join_issues(errors))


# ---------------- WMS goods receipt line ----------------
def _build_receipt(r: s.GoodsReceiptLineRow, res: Resolver) -> Built:
    values: dict[str, Any] = dict(
        source_system="WMS", source_record_id=r.source_record_id,
        receipt_number=r.receipt_number, receipt_line=r.receipt_line,
        po_number=r.po_number, po_line_number=r.po_line_number,
        source_location_code=r.location_code, source_product_ref=r.product_ref,
        accepted_qty=r.accepted_qty, uom=r.uom, received_at=r.received_at,
        posting_status=r.posting_status,
        accepted_qty_base=None, **_UNMAPPED_DERIVED,
    )
    errors: list[MappingError] = []
    loc = prod = None
    try:
        loc = res.location("WMS", r.location_code)
    except MappingError as e:
        errors.append(e)
    try:
        prod = res.to_base("WMS", r.product_ref, r.accepted_qty, r.uom)
    except MappingError as e:
        errors.append(e)
    if not errors:
        values.update(canonical_location_id=loc.canonical_location_id,
                      canonical_product_id=prod[0], base_uom=prod[1],
                      accepted_qty_base=prod[2], mapping_status="MAPPED")
    return Built(values, _join_issues(errors))


# ---------------- HRMS employee ----------------
def _build_employee(r: s.EmployeeRow, res: Resolver) -> Built:
    values: dict[str, Any] = dict(
        employee_id=r.source_employee_id, display_name=r.display_name,
        employment_status=r.employment_status, role=r.role,
        source_location_code=r.location_code, manager_employee_id=r.manager_employee_id,
        region_id=r.region_id, canonical_location_id=None, mapping_status="MAPPED",
    )
    if r.location_code:  # store-level employee: location MUST map before routing can use it
        try:
            loc = res.location("HRMS", r.location_code)
            values["canonical_location_id"] = loc.canonical_location_id
            values["region_id"] = r.region_id or loc.region_id
        except MappingError as e:
            values["mapping_status"] = "UNMAPPED"
            return Built(values, (e.code, e.detail))
    # no location_code -> region-scoped role: canonical_location_id stays NULL on purpose
    return Built(values)


# ---------------- WFM snapshot ----------------
def _build_snapshot(r: s.WorkforceStatusRow, _res: Resolver) -> Built:
    return Built(dict(
        employee_id=r.source_employee_id, availability_status=r.availability_status,
        shift_start=r.shift_start, shift_end=r.shift_end, observed_at=r.observed_at,
    ))


REGISTRY: dict[str, DatasetSpec] = {
    "product_aliases": DatasetSpec(
        "product_aliases", s.ProductAliasRow, ProductAlias, "external_item_id",
        ("tenant_id", "source_system", "external_item_id"),
        ("canonical_product_id", "source_uom", "base_uom", "to_base_factor"),
        _build_product_alias),
    "location_aliases": DatasetSpec(
        "location_aliases", s.LocationAliasRow, LocationAlias, "external_location_id",
        ("tenant_id", "source_system", "external_location_id"),
        ("canonical_location_id", "region_id"),
        _build_location_alias),
    "purchase_order_lines": DatasetSpec(
        "purchase_order_lines", s.PurchaseOrderLineRow, PoLine, "source_record_id",
        ("tenant_id", "source_system", "source_record_id"),
        ("po_number", "line_number", "supplier_code", "source_location_code",
         "source_item_code", "ordered_qty", "ordered_uom", "status", "expected_receipt_by"),
        _build_po,
        derived_fields=("canonical_product_id", "canonical_location_id", "base_uom",
                        "ordered_qty_base", "mapping_status")),
    "goods_receipt_lines": DatasetSpec(
        "goods_receipt_lines", s.GoodsReceiptLineRow, ReceiptLine, "source_record_id",
        ("tenant_id", "source_system", "source_record_id"),
        ("receipt_number", "receipt_line", "po_number", "po_line_number",
         "source_location_code", "source_product_ref", "accepted_qty", "uom",
         "received_at", "posting_status"),
        _build_receipt,
        derived_fields=("canonical_product_id", "canonical_location_id", "base_uom",
                        "accepted_qty_base", "mapping_status")),
    "employees": DatasetSpec(
        "employees", s.EmployeeRow, Employee, "source_employee_id",
        ("tenant_id", "employee_id"),
        ("display_name", "employment_status", "role", "source_location_code",
         "manager_employee_id", "region_id"),
        _build_employee,
        derived_fields=("canonical_location_id", "mapping_status"),
        on_change="update"),   # HRMS roster = current state; a termination MUST take effect
    "workforce_status": DatasetSpec(
        "workforce_status", s.WorkforceStatusRow, WorkforceSnapshot, "source_employee_id",
        ("tenant_id", "employee_id", "observed_at"),
        ("availability_status", "shift_start", "shift_end"),
        _build_snapshot),
}

# Order matters: crosswalks first, then facts that need them.
LOAD_ORDER = ["location_aliases", "product_aliases", "employees", "workforce_status",
              "purchase_order_lines", "goods_receipt_lines"]


def mapping_version() -> str:
    return get_settings().mapping_version
