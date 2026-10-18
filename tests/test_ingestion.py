"""Task A tests: validation, mapping, UOM, idempotency, conflicts, data quality."""
from decimal import Decimal

from sqlalchemy import func, select

from app.db.models import Employee, PoLine, ReceiptLine, RowIssue, WorkforceSnapshot
from app.ingestion.service import run_import
from tests.conftest import fixture_bytes


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def test_fixture_pack_loads_with_expected_counts(session, load_all):
    batches = {b.dataset: b for b in load_all()}
    po = batches["purchase_order_lines"]
    assert (po.total_rows, po.accepted_rows, po.duplicate_rows, po.rejected_rows) == (7, 6, 1, 0)
    gr = batches["goods_receipt_lines"]
    assert (gr.total_rows, gr.accepted_rows, gr.duplicate_rows) == (7, 6, 1)
    assert count(session, PoLine) == 6 and count(session, ReceiptLine) == 6
    assert count(session, Employee) == 6 and count(session, WorkforceSnapshot) == 6


def test_uom_converted_to_base(session, load_all):
    load_all()
    po505 = session.scalars(select(PoLine).where(PoLine.po_number == "PO-505")).one()
    assert po505.ordered_qty == Decimal("10") and po505.ordered_uom == "BOX"
    assert po505.ordered_qty_base == Decimal("120") and po505.base_uom == "EA"
    assert po505.canonical_product_id == "SKU-002" and po505.canonical_location_id == "STORE-015"


def test_regional_employee_keeps_no_store(session, load_all):
    load_all()
    e = session.scalars(select(Employee).where(Employee.employee_id == "HR-501")).one()
    assert e.canonical_location_id is None and e.region_id == "REGION-MH"
    assert e.mapping_status == "MAPPED"


def test_importing_everything_twice_changes_nothing(session, load_all):
    load_all()
    before = (count(session, PoLine), count(session, ReceiptLine), count(session, Employee))
    second = {b.dataset: b for b in load_all()}
    assert (count(session, PoLine), count(session, ReceiptLine), count(session, Employee)) == before
    assert second["goods_receipt_lines"].accepted_rows == 0
    assert second["goods_receipt_lines"].duplicate_rows == 7
    assert second["purchase_order_lines"].accepted_rows == 0


def test_conflicting_duplicate_id_is_flagged_not_overwritten(session, load_all):
    load_all()
    changed = fixture_bytes("goods_receipt_lines").replace(
        b"WMS-GRN501-A-L1,GRN-501-A,1,PO-501,1,WH-MH-014,WMS-BRC-001,60",
        b"WMS-GRN501-A-L1,GRN-501-A,1,PO-501,1,WH-MH-014,WMS-BRC-001,65")
    b = run_import(session, "goods_receipt_lines", changed, "changed.csv")
    assert b.conflict_rows == 1 and b.status == "COMPLETED_WITH_ISSUES"
    row = session.scalars(select(ReceiptLine).where(
        ReceiptLine.source_record_id == "WMS-GRN501-A-L1")).one()
    assert row.accepted_qty == Decimal("60")   # original kept
    issue = session.scalars(select(RowIssue).where(RowIssue.kind == "CONFLICT")).one()
    assert "accepted_qty: 60 -> 65" in issue.detail


def test_invalid_rows_are_rejected_with_reason(session, load_all):
    load_all()
    bad = (b"source_record_id,po_number,line_number,supplier_code,location_code,item_code,"
           b"ordered_qty,ordered_uom,status,expected_receipt_by\n"
           b"ERP-X1,PO-900,1,SUP-1,MUM-014,ERP-SKU-001,-5,EA,OPEN,2026-10-15T12:00:00+05:30\n"
           b"ERP-X2,PO-901,1,SUP-1,MUM-014,ERP-SKU-001,5,EA,OPEN,2026-10-15T12:00:00\n"   # naive time
           b"ERP-X3,PO-902,1,SUP-1,MUM-014,ERP-SKU-001,5,EA,WEIRD,2026-10-15T12:00:00+05:30\n")
    b = run_import(session, "purchase_order_lines", bad, "bad.csv")
    assert b.rejected_rows == 3 and b.accepted_rows == 0
    reasons = [i.detail for i in session.scalars(select(RowIssue).where(RowIssue.batch_id == b.id))]
    assert any("ordered_qty" in r for r in reasons)
    assert any("expected_receipt_by" in r for r in reasons)
    assert any("status" in r for r in reasons)


def test_unknown_product_is_stored_unmapped_and_reported(session, load_all):
    load_all()
    csv = (b"source_record_id,po_number,line_number,supplier_code,location_code,item_code,"
           b"ordered_qty,ordered_uom,status,expected_receipt_by\n"
           b"ERP-U1,PO-950,1,SUP-1,MUM-014,ERP-UNKNOWN,5,EA,OPEN,2026-10-15T12:00:00+05:30\n")
    b = run_import(session, "purchase_order_lines", csv, "u.csv")
    assert b.status == "COMPLETED_WITH_ISSUES"
    row = session.scalars(select(PoLine).where(PoLine.source_record_id == "ERP-U1")).one()
    assert row.mapping_status == "UNMAPPED" and row.canonical_product_id is None
    issue = session.scalars(select(RowIssue).where(RowIssue.kind == "UNMAPPED")).one()
    assert issue.reason_code == "UNMAPPED_PRODUCT"


def test_late_alias_resolves_unmapped_row_on_reimport(session, load_all):
    load_all()
    csv = (b"source_record_id,po_number,line_number,supplier_code,location_code,item_code,"
           b"ordered_qty,ordered_uom,status,expected_receipt_by\n"
           b"ERP-U1,PO-950,1,SUP-1,MUM-014,ERP-NEW,5,EA,OPEN,2026-10-15T12:00:00+05:30\n")
    run_import(session, "purchase_order_lines", csv, "u.csv")
    alias = (b"source_system,external_item_id,canonical_product_id,source_uom,base_uom,to_base_factor\n"
             b"ERP,ERP-NEW,SKU-009,EA,EA,1\n")
    run_import(session, "product_aliases", alias, "a.csv")
    b = run_import(session, "purchase_order_lines", csv, "u.csv")   # same file again
    assert b.duplicate_rows == 1 and b.updated_rows == 1
    row = session.scalars(select(PoLine).where(PoLine.source_record_id == "ERP-U1")).one()
    assert row.mapping_status == "MAPPED" and row.canonical_product_id == "SKU-009"
    assert session.scalars(select(RowIssue).where(RowIssue.kind == "UNMAPPED",
                                                  RowIssue.resolved.is_(False))).first() is None


def test_api_import_status_codes_and_data_quality(client):
    for ds in ("location_aliases", "product_aliases"):
        r = client.post(f"/imports/{ds}", files={"file": (f"{ds}.csv", fixture_bytes(ds), "text/csv")})
        assert r.status_code == 201 and r.json()["status"] == "COMPLETED"
    bad = b"source_system,external_item_id\nERP,X\n"
    r = client.post("/imports/product_aliases", files={"file": ("bad.csv", bad, "text/csv")})
    assert r.status_code == 422 and "missing required columns" in r.json()["detail"]
    assert client.post("/imports/nope", files={"file": ("x.csv", b"a\n1\n", "text/csv")}).status_code == 404
    r = client.post("/imports/purchase_order_lines",
                    files={"file": ("po.csv", fixture_bytes("purchase_order_lines"), "text/csv")})
    assert r.status_code == 201 and r.json()["duplicate_rows"] == 1
    assert client.get(f"/imports/{r.json()['id']}").json()["accepted_rows"] == 6
    dq = client.get("/data-quality").json()
    assert dq["open_issue_count"] == 1 and dq["by_kind"] == {"REJECTED": 1}   # the failed-columns batch
