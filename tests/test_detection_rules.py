"""Unit tests for the PURE shortfall rule (no database)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.detection.rules import Outcome, PoFacts, ReceiptFacts, build_finding, evaluate_line

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 10, 16, 15, 0, tzinfo=IST)
D = Decimal


def po(qty="100", status="OPEN", deadline=datetime(2026, 10, 15, 12, 0, tzinfo=IST), **kw):
    base = dict(id="p1", source_record_id="ERP-1", po_number="PO-1", line_number=1, supplier_code="S",
                status=status, expected_receipt_by=deadline, mapping_status="MAPPED",
                canonical_product_id="SKU-1", canonical_location_id="STORE-1", base_uom="EA",
                expected_qty_base=D(qty))
    base.update(kw)
    return PoFacts(**base)


def rc(sid, qty, status="POSTED", at=datetime(2026, 10, 15, 10, 0, tzinfo=IST), **kw):
    base = dict(id=sid, source_record_id=sid, receipt_number="G", po_number="PO-1", po_line_number=1,
                status=status, received_at=at, mapping_status="MAPPED", canonical_product_id="SKU-1",
                canonical_location_id="STORE-1", accepted_qty_base=D(qty))
    base.update(kw)
    return ReceiptFacts(**base)


def test_partial_receipts_are_summed():                      # PO-501
    r = evaluate_line(po("100"), [rc("a", "60"), rc("b", "32")], NOW)
    assert r.outcome is Outcome.SHORTFALL and r.variance == D("8") and r.received == D("92")


def test_exact_receipt_is_not_a_shortfall():                 # PO-502
    assert evaluate_line(po("40"), [rc("a", "40")], NOW).outcome is Outcome.NO_SHORTFALL


def test_uom_converted_before_comparison():                  # PO-505: 10 BOX = 120 EA vs 96 EA
    r = evaluate_line(po("120"), [rc("a", "96")], NOW)
    assert r.outcome is Outcome.SHORTFALL and r.variance == D("24")


def test_cancelled_line_never_raises():                      # PO-504
    r = evaluate_line(po("20", status="CANCELLED"), [], NOW)
    assert r.outcome is Outcome.EXCLUDED_CANCELLED


def test_not_actionable_before_deadline():                   # PO-503
    r = evaluate_line(po("50", deadline=datetime(2026, 10, 17, 18, 0, tzinfo=IST)), [rc("a", "20")], NOW)
    assert r.outcome is Outcome.NOT_YET_ACTIONABLE


def test_deadline_boundary_is_strict():
    at_deadline = po("10", deadline=NOW)
    assert evaluate_line(at_deadline, [], NOW).outcome is Outcome.NOT_YET_ACTIONABLE
    just_after = po("10", deadline=NOW - timedelta(seconds=1))
    assert evaluate_line(just_after, [], NOW).outcome is Outcome.SHORTFALL


def test_no_receipts_at_all_is_full_shortfall():
    r = evaluate_line(po("10"), [], NOW)
    assert r.outcome is Outcome.SHORTFALL and r.variance == D("10") and r.received == D("0")


def test_late_receipt_after_deadline_still_counts():
    r = evaluate_line(po("10"), [rc("a", "10", at=datetime(2026, 10, 16, 9, 0, tzinfo=IST))], NOW)
    assert r.outcome is Outcome.NO_SHORTFALL


def test_future_dated_and_unposted_receipts_not_counted():
    rs = [rc("a", "5"), rc("b", "5", status="PENDING"),
          rc("c", "5", at=NOW + timedelta(hours=1))]
    r = evaluate_line(po("20"), rs, NOW)
    assert r.received == D("5") and len(r.ignored) == 2


def test_tolerance_is_configurable():
    r = evaluate_line(po("100"), [rc("a", "98")], NOW, tolerance=D("2"))
    assert r.outcome is Outcome.NO_SHORTFALL
    assert evaluate_line(po("100"), [rc("a", "98")], NOW).outcome is Outcome.SHORTFALL


def test_over_receipt_is_not_a_shortfall():
    assert evaluate_line(po("10"), [rc("a", "12")], NOW).outcome is Outcome.NO_SHORTFALL


def test_unmapped_po_is_data_quality_not_exception():
    r = evaluate_line(po("10", mapping_status="UNMAPPED", expected_qty_base=None), [], NOW)
    assert r.outcome is Outcome.DQ_UNMAPPED


def test_unmapped_receipt_blocks_evaluation():
    r = evaluate_line(po("10"), [rc("a", "5", mapping_status="UNMAPPED", accepted_qty_base=None)], NOW)
    assert r.outcome is Outcome.DQ_UNMAPPED


def test_receipt_for_other_product_is_mismatch_not_guess():
    r = evaluate_line(po("10"), [rc("a", "5", canonical_product_id="SKU-9")], NOW)
    assert r.outcome is Outcome.DQ_MISMATCH


def test_conflicting_source_id_blocks_evaluation():
    r = evaluate_line(po("10"), [rc("a", "5")], NOW, conflicted_ids=frozenset({"a"}))
    assert r.outcome is Outcome.DQ_CONFLICT


def test_finding_is_neutral_about_cause():
    r = evaluate_line(po("100"), [rc("a", "92")], NOW)
    f = build_finding(r, NOW)
    text = " ".join(f["known"] + f["unknown"]).lower()
    assert "dispatch" in text and "not proof of supplier fault" in text
    assert "supplier shipped less" not in text and "lost in transit" not in text
    assert f["evidence_needed"]
