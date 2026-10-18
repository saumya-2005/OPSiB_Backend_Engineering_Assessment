"""DB layer for detection: load facts -> call pure rule -> upsert exception cases.

Idempotency / concurrency
-------------------------
The case is identified by case_key = RECEIPT_SHORTFALL:<PO>:<line>, protected by
UNIQUE(tenant_id, case_key). We insert with ON CONFLICT DO NOTHING; if the row already
exists we lock it (SELECT ... FOR UPDATE) and update it. Two concurrent runs therefore
end up with exactly ONE case; the loser just updates the same row.

Late / changing data
--------------------
Re-running detection re-evaluates every PO line at the frozen evaluation time:
  * still short, numbers changed  -> case updated, revision + 1
  * no longer short (late receipt / PO cancelled) -> OPEN case becomes AUTO_CLOSED
  * evidence is unmapped / conflicting / mismatched -> case left untouched, reported as
    data-quality (we never close or open a business case on unreliable evidence)
"""
import uuid
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import clock
from app.config import get_settings
from app.db.models import ExceptionCase, ExceptionEvidence, PoLine, ReceiptLine, RowIssue
from app.detection.rules import (
    EXCEPTION_TYPE,
    LineResult,
    Outcome,
    PoFacts,
    ReceiptFacts,
    build_finding,
    case_key,
    evaluate_line,
)


@dataclass
class LineReport:
    po_number: str
    line_number: int
    outcome: str
    reason: str
    expected: Decimal | None
    received: Decimal | None
    variance: Decimal | None
    exception_id: str | None
    case_action: str | None   # CREATED | UPDATED | UNCHANGED | AUTO_CLOSED | None


def _po_facts(p: PoLine) -> PoFacts:
    return PoFacts(p.id, p.source_record_id, p.po_number, p.line_number, p.supplier_code, p.status,
                   p.expected_receipt_by, p.mapping_status, p.canonical_product_id,
                   p.canonical_location_id, p.base_uom, p.ordered_qty_base)


def _receipt_facts(r: ReceiptLine) -> ReceiptFacts:
    return ReceiptFacts(r.id, r.source_record_id, r.receipt_number, r.po_number, r.po_line_number,
                        r.posting_status, r.received_at, r.mapping_status, r.canonical_product_id,
                        r.canonical_location_id, r.accepted_qty_base)


def run_detection(session: Session, tenant_id: str | None = None) -> list[LineReport]:
    settings = get_settings()
    tenant_id = tenant_id or settings.tenant_id
    now = clock.now()

    po_rows = session.scalars(select(PoLine).where(PoLine.tenant_id == tenant_id)
                              .order_by(PoLine.po_number, PoLine.line_number)).all()
    receipts: dict[tuple[str, int], list[ReceiptFacts]] = defaultdict(list)
    for r in session.scalars(select(ReceiptLine).where(ReceiptLine.tenant_id == tenant_id)
                             .order_by(ReceiptLine.received_at, ReceiptLine.source_record_id)):
        receipts[(r.po_number, r.po_line_number)].append(_receipt_facts(r))
    conflicted = frozenset(session.scalars(select(RowIssue.source_record_id).where(
        RowIssue.tenant_id == tenant_id, RowIssue.kind == "CONFLICT",
        RowIssue.resolved.is_(False), RowIssue.source_record_id.is_not(None))))

    by_line: dict[tuple[str, int], list[PoLine]] = defaultdict(list)
    for p in po_rows:
        by_line[(p.po_number, p.line_number)].append(p)

    reports: list[LineReport] = []
    for (po_no, line_no), rows in by_line.items():
        if len(rows) > 1:  # two different source ids claim the same PO line: ambiguous identity
            res = LineResult(Outcome.DQ_CONFLICT,
                             f"{len(rows)} PO source records share PO {po_no} line {line_no}: "
                             + ", ".join(sorted(x.source_record_id for x in rows)), _po_facts(rows[0]))
        else:
            res = evaluate_line(_po_facts(rows[0]), receipts.get((po_no, line_no), []), now,
                                settings.shortfall_tolerance_base, conflicted)
        exc_id, action = _apply(session, res, tenant_id, now)
        reports.append(LineReport(po_no, line_no, res.outcome.value, res.reason, res.expected,
                                  res.received, res.variance, exc_id, action))
    session.commit()
    return reports


def _apply(session: Session, res: LineResult, tenant_id: str, now):
    po = res.po
    key = case_key(po.po_number, po.line_number)

    if res.outcome is Outcome.SHORTFALL:
        return _upsert_case(session, res, tenant_id, key, now)

    existing = session.scalars(select(ExceptionCase).where(
        ExceptionCase.tenant_id == tenant_id, ExceptionCase.case_key == key)
        .with_for_update()).one_or_none()
    if existing is None:
        return None, None
    # Only "business says it is fine" outcomes may close a case. Data-quality /
    # not-yet-actionable outcomes never change a case.
    if res.outcome in (Outcome.NO_SHORTFALL, Outcome.EXCLUDED_CANCELLED) and existing.status == "OPEN":
        existing.status = "AUTO_CLOSED"
        existing.close_reason = f"{res.reason} at {now.isoformat()}"
        existing.last_evaluated_at = now
        existing.revision += 1
        return existing.id, "AUTO_CLOSED"
    return existing.id, "UNCHANGED"


def _upsert_case(session: Session, res: LineResult, tenant_id: str, key: str, now):
    po = res.po
    settings = get_settings()
    finding = build_finding(res, now)
    values = dict(
        exception_type=EXCEPTION_TYPE, rule_version=settings.rule_version,
        mapping_version=settings.mapping_version, last_evaluated_at=now,
        po_number=po.po_number, po_line_number=po.line_number, supplier_code=po.supplier_code,
        canonical_product_id=po.canonical_product_id, canonical_location_id=po.canonical_location_id,
        expected_qty=res.expected, received_qty=res.received, variance_qty=res.variance,
        unit=po.base_uom, expected_receipt_by=po.expected_receipt_by, summary=finding["summary"],
        known_facts=finding["known"], unknowns=finding["unknown"],
        evidence_needed=finding["evidence_needed"],
    )
    new_id = str(uuid.uuid4())
    inserted = session.execute(
        pg_insert(ExceptionCase)
        .values(id=new_id, tenant_id=tenant_id, case_key=key, status="OPEN",
                detected_at=now, revision=1, **values)
        .on_conflict_do_nothing(index_elements=["tenant_id", "case_key"])
        .returning(ExceptionCase.id)).scalar_one_or_none()

    if inserted:
        case = session.get(ExceptionCase, new_id)
        action = "CREATED"
    else:
        case = session.scalars(select(ExceptionCase).where(
            ExceptionCase.tenant_id == tenant_id, ExceptionCase.case_key == key)
            .with_for_update()).one()
        if case.status == "RESOLVED":          # human-verified: never silently reopened/changed
            return case.id, "UNCHANGED"
        changed = (case.expected_qty != res.expected or case.received_qty != res.received
                   or case.status != "OPEN" or case.rule_version != settings.rule_version)
        if not changed:
            case.last_evaluated_at = now
            return case.id, "UNCHANGED"
        for k, v in values.items():
            setattr(case, k, v)
        case.status = "OPEN"
        case.close_reason = None
        case.revision += 1
        action = "UPDATED"

    _replace_evidence(session, case, res, session.get(PoLine, po.id))
    return case.id, action


def _replace_evidence(session: Session, case: ExceptionCase, res: LineResult, po_row: PoLine):
    case.evidence.clear()
    session.flush()
    case.evidence.append(ExceptionEvidence(
        role="PO_LINE", record_type="po_line", record_id=po_row.id, source_system=po_row.source_system,
        source_record_id=po_row.source_record_id, import_batch_id=po_row.import_batch_id,
        qty_base=po_row.ordered_qty_base))
    rows = {r.id: r for r in session.scalars(select(ReceiptLine).where(
        ReceiptLine.id.in_([r.id for r in res.counted] + [r.id for r, _ in res.ignored])))}
    for r in res.counted:
        row = rows[r.id]
        case.evidence.append(ExceptionEvidence(
            role="RECEIPT_COUNTED", record_type="receipt_line", record_id=row.id,
            source_system=row.source_system, source_record_id=row.source_record_id,
            import_batch_id=row.import_batch_id, qty_base=row.accepted_qty_base))
    for r, why in res.ignored:
        row = rows[r.id]
        case.evidence.append(ExceptionEvidence(
            role="RECEIPT_IGNORED", record_type="receipt_line", record_id=row.id,
            source_system=row.source_system, source_record_id=row.source_record_id,
            import_batch_id=row.import_batch_id, qty_base=row.accepted_qty_base, note=why))
