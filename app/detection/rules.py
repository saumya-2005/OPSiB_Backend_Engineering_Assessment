"""Receipt-shortfall rule: PURE functions, no database, no clock, no I/O.

Everything the rule needs is passed in (including `now`), so it is deterministic and
unit-testable on its own. The DB layer (service.py) loads facts and calls this.

Rule (receipt-shortfall-v1)
---------------------------
For one PO line:
  1. CANCELLED / non-OPEN line                        -> excluded, never an exception
  2. PO line mapping incomplete                       -> data-quality, NOT a business exception
  3. now <= expected_receipt_by (window still open)   -> not actionable yet
  4. collect receipt lines linked by (po_number, po_line_number):
        ignore if received_at > now   (not knowable at evaluation time)
        ignore if posting_status != POSTED
  5. any counted receipt unmapped / for another product or location / any source id
     in an open conflict                              -> data-quality, not evaluated
  6. expected = PO qty in BASE uom; received = sum(accepted qty in BASE uom)
     variance = expected - received;  shortfall iff variance > tolerance

Deadline boundary: the window has "passed" only when now is STRICTLY after the
deadline. At exactly the deadline it is still open.
Late receipts (after the deadline but at/before `now`) DO count: if they fill the
order there is nothing left to investigate.
"""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum

EXCEPTION_TYPE = "RECEIPT_SHORTFALL"


class Outcome(str, Enum):
    SHORTFALL = "SHORTFALL"
    NO_SHORTFALL = "NO_SHORTFALL"
    EXCLUDED_CANCELLED = "EXCLUDED_CANCELLED"
    EXCLUDED_STATUS = "EXCLUDED_STATUS"
    NOT_YET_ACTIONABLE = "NOT_YET_ACTIONABLE"
    DQ_UNMAPPED = "DATA_QUALITY_UNMAPPED"
    DQ_CONFLICT = "DATA_QUALITY_CONFLICT"
    DQ_MISMATCH = "DATA_QUALITY_MISMATCH"


@dataclass(frozen=True)
class PoFacts:
    id: str
    source_record_id: str
    po_number: str
    line_number: int
    supplier_code: str
    status: str
    expected_receipt_by: datetime
    mapping_status: str
    canonical_product_id: str | None
    canonical_location_id: str | None
    base_uom: str | None
    expected_qty_base: Decimal | None


@dataclass(frozen=True)
class ReceiptFacts:
    id: str
    source_record_id: str
    receipt_number: str
    po_number: str
    po_line_number: int
    status: str            # posting_status
    received_at: datetime
    mapping_status: str
    canonical_product_id: str | None
    canonical_location_id: str | None
    accepted_qty_base: Decimal | None


@dataclass
class LineResult:
    outcome: Outcome
    reason: str
    po: PoFacts
    expected: Decimal | None = None
    received: Decimal | None = None
    variance: Decimal | None = None
    counted: list[ReceiptFacts] = field(default_factory=list)
    ignored: list[tuple[ReceiptFacts, str]] = field(default_factory=list)


def case_key(po_number: str, line_number: int) -> str:
    """Stable business key: one case per PO line, independent of rule version or run."""
    return f"{EXCEPTION_TYPE}:{po_number}:{line_number}"


def fmt(d: Decimal) -> str:
    return format(d.normalize(), "f")


def evaluate_line(po: PoFacts, receipts: list[ReceiptFacts], now: datetime,
                  tolerance: Decimal = Decimal("0"),
                  conflicted_ids: frozenset[str] = frozenset()) -> LineResult:
    if po.status == "CANCELLED":
        return LineResult(Outcome.EXCLUDED_CANCELLED, "PO line is CANCELLED", po)
    if po.status != "OPEN":
        return LineResult(Outcome.EXCLUDED_STATUS, f"PO line status {po.status} is not evaluated", po)

    if po.mapping_status != "MAPPED" or po.expected_qty_base is None:
        return LineResult(Outcome.DQ_UNMAPPED, "PO line product/location mapping incomplete", po)

    if po.source_record_id in conflicted_ids:
        return LineResult(Outcome.DQ_CONFLICT, "PO line has an unresolved conflicting duplicate id", po)

    if not now > po.expected_receipt_by:
        return LineResult(Outcome.NOT_YET_ACTIONABLE,
                          f"expected-receipt window open until {po.expected_receipt_by.isoformat()}", po,
                          expected=po.expected_qty_base)

    counted: list[ReceiptFacts] = []
    ignored: list[tuple[ReceiptFacts, str]] = []
    for r in receipts:
        if r.received_at > now:
            ignored.append((r, "received after evaluation time"))
        elif r.status != "POSTED":
            ignored.append((r, f"posting status {r.status} (not POSTED)"))
        else:
            counted.append(r)

    for r in counted:
        if r.source_record_id in conflicted_ids:
            return LineResult(Outcome.DQ_CONFLICT,
                              f"receipt {r.source_record_id} has an unresolved conflicting duplicate id",
                              po, counted=counted, ignored=ignored)
        if r.mapping_status != "MAPPED" or r.accepted_qty_base is None:
            return LineResult(Outcome.DQ_UNMAPPED,
                              f"receipt {r.source_record_id} product/location mapping incomplete",
                              po, counted=counted, ignored=ignored)
        if (r.canonical_product_id != po.canonical_product_id
                or r.canonical_location_id != po.canonical_location_id):
            return LineResult(Outcome.DQ_MISMATCH,
                              f"receipt {r.source_record_id} is for a different product or location "
                              f"than the PO line", po, counted=counted, ignored=ignored)

    received = sum((r.accepted_qty_base for r in counted), Decimal("0"))
    variance = po.expected_qty_base - received
    base = dict(expected=po.expected_qty_base, received=received, variance=variance,
                counted=counted, ignored=ignored)
    if variance > tolerance:
        return LineResult(Outcome.SHORTFALL, "posted receipts are below ordered quantity", po, **base)
    reason = "fully received" if variance == 0 else (
        "within tolerance" if variance > 0 else "over-received")
    return LineResult(Outcome.NO_SHORTFALL, reason, po, **base)


def build_finding(res: LineResult, now: datetime) -> dict:
    """Neutral explanation: what IS known and what is NOT. Never asserts cause."""
    po = res.po
    uom = po.base_uom
    known = [
        f"PO {po.po_number} line {po.line_number} ordered {fmt(res.expected)} {uom} "
        f"(base units), expected by {po.expected_receipt_by.isoformat()}.",
        f"{len(res.counted)} posted receipt line(s) linked to this PO line total "
        f"{fmt(res.received)} {uom} as of {now.isoformat()}.",
        f"Posted receipts are {fmt(res.variance)} {uom} below the ordered quantity "
        f"after the expected-receipt deadline.",
    ]
    if res.ignored:
        known.append(f"{len(res.ignored)} linked receipt line(s) were not counted: "
                     + "; ".join(f"{r.source_record_id} ({why})" for r, why in res.ignored) + ".")
    unknown = [
        "Whether the supplier dispatched the full ordered quantity: no dispatch/ASN data is available.",
        "Whether any quantity was lost, damaged or delayed in transit, and where the difference arose.",
        "Whether further receipts are still expected or the line has been short-closed with the supplier.",
        "No cause is established. This is a receipt shortfall against the PO, not proof of supplier fault "
        "or financial loss.",
    ]
    evidence_needed = [
        f"Supplier dispatch / ASN quantity for PO {po.po_number} line {po.line_number}",
        "Carrier delivery / proof-of-delivery records",
        "Warehouse receiving records, including unposted or pending goods receipts",
        "Supplier or buyer confirmation of any short-close or planned follow-up delivery",
    ]
    summary = (f"Receipt shortfall of {fmt(res.variance)} {uom}: PO {po.po_number} L{po.line_number} "
               f"expected {fmt(res.expected)} {uom}, posted receipts {fmt(res.received)} {uom}. "
               f"Cause not established.")
    return {"summary": summary, "known": known, "unknown": unknown, "evidence_needed": evidence_needed}
