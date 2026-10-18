"""Integration: load the REAL fixture pack, run detection, check the six required outcomes."""
from decimal import Decimal

from sqlalchemy import func, select

from app.db.models import ExceptionCase, ExceptionEvidence
from app.detection.service import run_detection
from app.ingestion.service import run_import
from tests.conftest import fixture_bytes

D = Decimal


def by_po(reports):
    return {r.po_number: r for r in reports}


def test_required_scenarios_on_fixture_pack(session, load_all):
    load_all()
    out = by_po(run_detection(session))

    assert out["PO-501"].outcome == "SHORTFALL" and out["PO-501"].variance == D("8")
    assert out["PO-502"].outcome == "NO_SHORTFALL"
    assert out["PO-503"].outcome == "NOT_YET_ACTIONABLE"
    assert out["PO-504"].outcome == "EXCLUDED_CANCELLED"
    assert out["PO-505"].outcome == "SHORTFALL" and out["PO-505"].variance == D("24")
    assert out["PO-505"].expected == D("120") and out["PO-505"].received == D("96")
    assert out["PO-506"].outcome == "SHORTFALL" and out["PO-506"].variance == D("10")  # not 0

    cases = session.scalars(select(ExceptionCase).order_by(ExceptionCase.po_number)).all()
    assert [c.po_number for c in cases] == ["PO-501", "PO-505", "PO-506"]
    assert {c.case_key for c in cases} == {
        "RECEIPT_SHORTFALL:PO-501:1", "RECEIPT_SHORTFALL:PO-505:1", "RECEIPT_SHORTFALL:PO-506:1"}


def test_finding_contents_and_lineage(session, load_all):
    load_all()
    run_detection(session)
    c = session.scalars(select(ExceptionCase).where(ExceptionCase.po_number == "PO-501")).one()
    assert (c.canonical_product_id, c.canonical_location_id) == ("SKU-001", "STORE-014")
    assert (c.expected_qty, c.received_qty, c.variance_qty, c.unit) == (D("100"), D("92"), D("8"), "EA")
    assert c.rule_version == "receipt-shortfall-v1" and c.mapping_version == "map-v1"
    assert "not proof of supplier fault" in " ".join(c.unknowns)
    assert any("dispatch" in e.lower() or "ASN" in e for e in c.evidence_needed)
    ids = sorted((e.role, e.source_record_id) for e in c.evidence)
    assert ids == [("PO_LINE", "ERP-PO501-L1"),
                   ("RECEIPT_COUNTED", "WMS-GRN501-A-L1"), ("RECEIPT_COUNTED", "WMS-GRN501-B-L1")]


def test_duplicate_receipt_row_counted_once(session, load_all):
    load_all()
    run_detection(session)
    c = session.scalars(select(ExceptionCase).where(ExceptionCase.po_number == "PO-506")).one()
    counted = [e for e in c.evidence if e.role == "RECEIPT_COUNTED"]
    assert len(counted) == 1 and c.received_qty == D("10")


def test_running_detection_and_imports_twice_creates_no_duplicates(session, load_all):
    load_all()
    first = run_detection(session)
    load_all()                        # re-import everything
    second = run_detection(session)   # re-detect
    assert session.scalar(select(func.count()).select_from(ExceptionCase)) == 3
    assert session.scalar(select(func.count()).select_from(ExceptionEvidence)) == 7  # 501: PO+2 receipts, 505: PO+1, 506: PO+1
    assert all(r.case_action in (None, "UNCHANGED") for r in second)
    assert {(r.po_number, r.outcome) for r in first} == {(r.po_number, r.outcome) for r in second}
    assert all(c.revision == 1 for c in session.scalars(select(ExceptionCase)))


def test_late_receipt_closes_case_automatically(session, load_all):
    load_all()
    run_detection(session)
    late = (b"source_record_id,receipt_number,receipt_line,po_number,po_line_number,location_code,"
            b"product_ref,accepted_qty,uom,received_at,posting_status\n"
            b"WMS-GRN501-C-L1,GRN-501-C,1,PO-501,1,WH-MH-014,WMS-BRC-001,8,EA,"
            b"2026-10-16T12:00:00+05:30,POSTED\n")
    run_import(session, "goods_receipt_lines", late, "late.csv")
    out = by_po(run_detection(session))
    assert out["PO-501"].outcome == "NO_SHORTFALL" and out["PO-501"].case_action == "AUTO_CLOSED"
    c = session.scalars(select(ExceptionCase).where(ExceptionCase.po_number == "PO-501")).one()
    assert c.status == "AUTO_CLOSED" and "fully received" in c.close_reason


def test_partial_late_receipt_updates_same_case(session, load_all):
    load_all()
    run_detection(session)
    late = (b"source_record_id,receipt_number,receipt_line,po_number,po_line_number,location_code,"
            b"product_ref,accepted_qty,uom,received_at,posting_status\n"
            b"WMS-GRN501-C-L1,GRN-501-C,1,PO-501,1,WH-MH-014,WMS-BRC-001,3,EA,"
            b"2026-10-16T12:00:00+05:30,POSTED\n")
    run_import(session, "goods_receipt_lines", late, "late.csv")
    run_detection(session)
    cases = session.scalars(select(ExceptionCase).where(ExceptionCase.po_number == "PO-501")).all()
    assert len(cases) == 1 and cases[0].variance_qty == D("5") and cases[0].revision == 2


def test_unknown_product_mapping_is_data_quality_not_exception(session, load_all):
    load_all()
    po = (b"source_record_id,po_number,line_number,supplier_code,location_code,item_code,"
          b"ordered_qty,ordered_uom,status,expected_receipt_by\n"
          b"ERP-PO700-L1,PO-700,1,SUP-1,MUM-014,ERP-UNKNOWN,5,EA,OPEN,2026-10-15T12:00:00+05:30\n")
    run_import(session, "purchase_order_lines", po, "po700.csv")
    out = by_po(run_detection(session))
    assert out["PO-700"].outcome == "DATA_QUALITY_UNMAPPED" and out["PO-700"].exception_id is None
    assert session.scalar(select(func.count()).select_from(ExceptionCase)) == 3


def test_conflicting_receipt_id_blocks_evaluation_and_keeps_case(session, load_all):
    load_all()
    run_detection(session)
    changed = fixture_bytes("goods_receipt_lines").replace(
        b"WMS-GRN501-A-L1,GRN-501-A,1,PO-501,1,WH-MH-014,WMS-BRC-001,60",
        b"WMS-GRN501-A-L1,GRN-501-A,1,PO-501,1,WH-MH-014,WMS-BRC-001,65")
    run_import(session, "goods_receipt_lines", changed, "changed.csv")
    out = by_po(run_detection(session))
    assert out["PO-501"].outcome == "DATA_QUALITY_CONFLICT" and out["PO-501"].case_action == "UNCHANGED"
    c = session.scalars(select(ExceptionCase).where(ExceptionCase.po_number == "PO-501")).one()
    assert c.status == "OPEN" and c.variance_qty == D("8")


def test_api_endpoints(client):
    from tests.conftest import FIXTURES
    for ds in ("location_aliases", "product_aliases", "employees", "workforce_status",
               "purchase_order_lines", "goods_receipt_lines"):
        r = client.post(f"/imports/{ds}", files={"file": (f"{ds}.csv", (FIXTURES / f"{ds}.csv").read_bytes())})
        assert r.status_code in (201, 207)
    run = client.post("/detection/run").json()
    assert {l["po_number"]: l["outcome"] for l in run["lines"]}["PO-505"] == "SHORTFALL"
    lst = client.get("/exceptions").json()
    assert [e["po_number"] for e in lst] == ["PO-501", "PO-505", "PO-506"]
    one = client.get(f"/exceptions/{lst[0]['id']}").json()
    assert len(one["evidence"]) == 3 and one["unit"] == "EA"
    assert client.get("/exceptions/nope").status_code == 404
    assert client.get("/exceptions", params={"location": "STORE-015"}).json()[0]["po_number"] == "PO-505"
