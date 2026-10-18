"""Task C routing: pure policy tests + fixture-pack integration."""
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.db.models import Employee, Task, TaskAssignment, TaskEvent
from app.detection.service import run_detection
from app.ingestion.service import run_import
from app.routing.policy import POLICIES, Candidate, select_assignee
from app.routing.service import generate_tasks
from tests.conftest import fixture_bytes

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 10, 16, 15, 0, tzinfo=IST)
POLICY = POLICIES["RECEIPT_SHORTFALL"]


def cand(eid, role="receiving_associate", emp="ACTIVE", loc="STORE-014", avail="ON_SHIFT",
         observed=NOW - timedelta(minutes=10), start=NOW - timedelta(hours=6), end=NOW + timedelta(hours=3)):
    return Candidate(eid, role, emp, loc, avail, start, end, observed)


def pick(cands, loc="STORE-014"):
    return select_assignee(cands, POLICY, loc, NOW, 60)


# ------------------------------------------------------------------ pure policy
def test_eligible_receiver_selected_and_others_explained():
    d = pick([cand("HR-101", avail="OFF_SHIFT"), cand("HR-102"), cand("HR-103", emp="INACTIVE")])
    assert d.assignee_id == "HR-102" and d.tier == "PRIMARY"
    assert "HR-101 (availability OFF_SHIFT)" in d.reason
    assert "HR-103 (employment status INACTIVE" in d.reason


def test_inactive_employee_loses_even_though_shift_says_on_shift():
    d = pick([cand("HR-103", emp="INACTIVE", avail="ON_SHIFT")])
    assert d.assignee_id is None and d.tier == "NONE"


def test_off_shift_employee_not_assigned():
    assert pick([cand("HR-101", avail="OFF_SHIFT")]).assignee_id is None


def test_fallback_store_manager_when_no_receiver():
    d = pick([cand("HR-401", role="store_manager", loc="STORE-015")], loc="STORE-015")
    assert d.assignee_id == "HR-401" and d.tier == "FALLBACK"
    assert "no eligible receiving_associate" in d.reason


def test_fallback_not_used_while_primary_is_available():
    d = pick([cand("HR-102"), cand("HR-201", role="store_manager")])
    assert d.assignee_id == "HR-102"
    assert all(t["tier"] == "PRIMARY" for t in d.trace)


def test_no_approved_fallback_keeps_task_in_manual_queue():
    d = pick([cand("HR-401", role="store_manager", emp="INACTIVE")])
    assert d.assignee_id is None and "manual assignment required" in d.reason


def test_regional_role_is_not_an_approved_fallback():
    d = pick([cand("HR-501", role="regional_operations", loc=None)])
    assert d.assignee_id is None


def test_stale_snapshot_rejected():
    d = pick([cand("HR-102", observed=NOW - timedelta(minutes=61))])
    assert d.assignee_id is None and "stale" in d.trace[0]["reason"]


def test_snapshot_exactly_at_max_age_is_still_fresh():
    assert pick([cand("HR-102", observed=NOW - timedelta(minutes=60))]).assignee_id == "HR-102"


def test_outside_shift_window_rejected():
    d = pick([cand("HR-102", start=NOW + timedelta(hours=1), end=NOW + timedelta(hours=9))])
    assert d.assignee_id is None and d.trace[0]["reason"] == "outside shift window"


def test_missing_snapshot_rejected():
    d = pick([Candidate("HR-102", "receiving_associate", "ACTIVE", "STORE-014", None, None, None, None)])
    assert d.assignee_id is None and "no availability snapshot" in d.trace[0]["reason"]


def test_employee_at_another_location_rejected():
    assert pick([cand("HR-102", loc="STORE-015")]).assignee_id is None


def test_selection_is_deterministic_lowest_employee_id():
    assert pick([cand("HR-109"), cand("HR-102")]).assignee_id == "HR-102"


# ------------------------------------------------------------------ integration
def setup_world(session, load_all):
    load_all()
    run_detection(session)
    return generate_tasks(session)


def task_for(session, po):
    return session.scalars(select(Task).where(Task.task_key.like(f"%:{po}:1:%"))).one()


def test_hrms_location_codes_are_mapped_before_routing(session, load_all):
    load_all()
    emps = {e.employee_id: e for e in session.scalars(select(Employee))}
    assert emps["HR-102"].source_location_code == "ST-014" and emps["HR-102"].canonical_location_id == "STORE-014"
    assert emps["HR-401"].canonical_location_id == "STORE-015"
    assert emps["HR-501"].canonical_location_id is None       # regional: no store forced on them


def test_fixture_routing_matches_brief(session, load_all):
    setup_world(session, load_all)
    assert task_for(session, "PO-501").assignee_id == "HR-102"
    assert task_for(session, "PO-506").assignee_id == "HR-102"
    assert task_for(session, "PO-505").assignee_id == "HR-401"          # STORE-015 fallback manager
    assert {t.assignee_id for t in session.scalars(select(Task))} == {"HR-102", "HR-401"}  # never HR-101/HR-103


def test_assignment_reason_and_trace_are_persisted(session, load_all):
    setup_world(session, load_all)
    a = session.scalars(select(TaskAssignment).join(Task).where(Task.task_key.like("%:PO-501:1:%"))).one()
    assert a.tier == "PRIMARY" and a.assigned_by == "SYSTEM" and a.policy_version == "routing-v1"
    assert "HR-101 (availability OFF_SHIFT)" in a.reason and "HR-103" in a.reason and "INACTIVE" in a.reason
    verdicts = {t["employee_id"]: t["eligible"] for t in a.selection_trace}
    assert verdicts == {"HR-101": False, "HR-102": True, "HR-103": False}


def test_generating_tasks_twice_creates_nothing_new(session, load_all):
    setup_world(session, load_all)
    n_events = session.scalar(select(func.count()).select_from(TaskEvent))
    again = generate_tasks(session)
    assert session.scalar(select(func.count()).select_from(Task)) == 3
    assert session.scalar(select(func.count()).select_from(TaskEvent)) == n_events
    assert all(g.action == "UNCHANGED" for g in again)


def test_no_eligible_assignee_is_visible_then_assigned_when_roster_fixes(session, load_all):
    load_all()
    run_detection(session)
    roster = fixture_bytes("employees")
    run_import(session, "employees", roster.replace(b"HR-401,Rohan Iyer,ACTIVE", b"HR-401,Rohan Iyer,INACTIVE"),
               "emp_inactive.csv")
    first = {g.case_key: g for g in generate_tasks(session)}
    g = first["RECEIPT_SHORTFALL:PO-505:1"]
    assert g.action == "CREATED" and g.status == "CREATED" and g.assignee_id is None   # NOT silently assigned
    t = task_for(session, "PO-505")
    assert t.assignee_id is None

    assert {x.action for x in generate_tasks(session) if x.case_key.endswith("PO-505:1")} == {"STILL_UNASSIGNED"}

    run_import(session, "employees", roster, "emp_ok.csv")             # roster corrected
    fixed = {x.case_key: x for x in generate_tasks(session)}["RECEIPT_SHORTFALL:PO-505:1"]
    assert fixed.action == "ASSIGNED_LATE" and fixed.assignee_id == "HR-401"
    hist = session.scalars(select(TaskAssignment).where(TaskAssignment.task_id == t.id)
                           .order_by(TaskAssignment.assigned_at, TaskAssignment.ended_at.is_(None))).all()
    assert [(h.tier, h.assignee_id) for h in hist] == [("NONE", None), ("FALLBACK", "HR-401")]
    assert hist[0].ended_at is not None and hist[1].ended_at is None


def test_two_concurrent_workers_create_one_task_per_exception(engine, session, load_all):
    load_all()
    run_detection(session)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    errors = []

    def worker():
        try:
            with maker() as s:
                generate_tasks(s)
        except Exception as e:       # pragma: no cover - would fail the test below
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert session.scalar(select(func.count()).select_from(Task)) == 3
    for t in session.scalars(select(Task)):
        types = [e.event_type for e in session.scalars(
            select(TaskEvent).where(TaskEvent.task_id == t.id).order_by(TaskEvent.seq))]
        assert types == ["TASK_CREATED", "ASSIGNED"]
