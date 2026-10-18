"""Identity resolution: source IDs -> canonical IDs, and unit conversion.

Three different things, kept separate on purpose:
  1. field mapping    (column names)           -> schemas.py
  2. entity identity  (ERP-SKU-001 == WMS-BRC-001 == SKU-001)  -> here, via alias tables
  3. business meaning (what a shortfall means) -> detection/

A missing or ambiguous mapping raises MappingError. The caller stores the row as
UNMAPPED + a data-quality issue. We never guess (e.g. no name/text similarity).
"""
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import LocationAlias, ProductAlias


class MappingError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ProductMatch:
    canonical_product_id: str
    source_uom: str
    base_uom: str
    to_base_factor: Decimal


@dataclass(frozen=True)
class LocationMatch:
    canonical_location_id: str
    region_id: str


class Resolver:
    """Snapshot of the crosswalks for one tenant (loaded once per import batch)."""

    def __init__(self, session: Session, tenant_id: str):
        self._products = {
            (p.source_system, p.external_item_id): ProductMatch(
                p.canonical_product_id, p.source_uom, p.base_uom, p.to_base_factor
            )
            for p in session.scalars(select(ProductAlias).where(ProductAlias.tenant_id == tenant_id))
        }
        self._locations = {
            (a.source_system, a.external_location_id): LocationMatch(a.canonical_location_id, a.region_id)
            for a in session.scalars(select(LocationAlias).where(LocationAlias.tenant_id == tenant_id))
        }

    def location(self, source_system: str, external_id: str) -> LocationMatch:
        m = self._locations.get((source_system, external_id))
        if m is None:
            raise MappingError("UNMAPPED_LOCATION", f"no {source_system} location alias for '{external_id}'")
        return m

    def product(self, source_system: str, external_id: str) -> ProductMatch:
        m = self._products.get((source_system, external_id))
        if m is None:
            raise MappingError("UNMAPPED_PRODUCT", f"no {source_system} product alias for '{external_id}'")
        return m

    def to_base(self, source_system: str, external_id: str, qty: Decimal, uom: str):
        """Return (canonical_product_id, base_uom, qty_in_base_uom).

        The row's UOM must equal the alias' declared source_uom. If it does not
        (e.g. row says CASE, alias knows BOX) the unit is ambiguous -> error.
        """
        m = self.product(source_system, external_id)
        if uom != m.source_uom:
            raise MappingError(
                "UOM_MISMATCH",
                f"row UOM '{uom}' differs from mapped source UOM '{m.source_uom}' for '{external_id}'",
            )
        return m.canonical_product_id, m.base_uom, qty * m.to_base_factor
