"""Build the minimal evidence package for one exception (no employee data, no PII)."""
from datetime import timezone

from app.ai.summary import EvidencePackage, EvidenceRecord
from app.db.models import ExceptionCase
from app.detection.rules import fmt


def package_from_case(case: ExceptionCase) -> EvidencePackage:
    return EvidencePackage(
        case_key=case.case_key, exception_type=case.exception_type,
        facts={"po_number": case.po_number, "po_line": str(case.po_line_number),
               "product": case.canonical_product_id, "location": case.canonical_location_id,
               "expected": f"{fmt(case.expected_qty)} {case.unit}",
               "received": f"{fmt(case.received_qty)} {case.unit}",
               "variance": f"{fmt(case.variance_qty)} {case.unit}",
               # always UTC: the DB session timezone must not change what the model (or the number check) sees
               "expected_by": case.expected_receipt_by.astimezone(timezone.utc).isoformat()},
        known=list(case.known_facts), unknowns=list(case.unknowns),
        evidence_needed=list(case.evidence_needed),
        evidence=[EvidenceRecord(ref=f"{e.role}:{e.source_record_id}", role=e.role,
                                 source_system=e.source_system, source_record_id=e.source_record_id,
                                 qty_base=None if e.qty_base is None else fmt(e.qty_base), note=e.note)
                  for e in case.evidence])