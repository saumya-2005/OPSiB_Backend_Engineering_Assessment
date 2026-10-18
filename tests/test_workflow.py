"""Task C workflow: state machine, authorisation, audit trail, retries, reassignment."""
import threading

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.models import ExceptionCase, Task, TaskAssignment, TaskEvent
from app.detection.service import run_detection
from app.routing.service import generate_tasks
from app.workflows.service import perform
from app.workflows.state_machine import TRANSITIONS, Action, InvalidTransition, Status, next_status

DONE = {"resolution_code": "QUANTITY_CONFIRMED_SHORT", "evidence_ref": "GRN-AUDIT-7781"}


# ------------------------------------------------------------------ pure state machine
def test_happy_path_is_the_documented_chain():
    s = Status.CREATED
    for a in (Action.ASSIGN, Action.ACKNOWLEDGE, Action.START, Action.COMPLETE,
              Action.SUBMIT_FOR_VERIFICATION, Action.VERIFY):
        s = next_status(s, a)
    assert s is Status.VERIFIED


@pytest.mark.parametrize("status,action", [
    (Status.CREATED, Action.START), (Status.ASSIGNED, Action.START), (Status.ASSIGNED, Action.COMPLETE),
    (Status.ACKNOWLEDGED, Action.COMPLETE), (Status.IN_PROGRESS, Action.VERIFY),
    (Status.PENDING_VERIFICATION, Action.START), (Status.VERIFIED, Action.REJECT),
    (Status.VERIFIED, Action.REASSIGN), (Status.PENDING_VERIFICATION, Action.REASSIGN),
    (Status.BLOCKED, Action.COMPLETE),
])
def test_illegal_transitions_raise(status, action):
    with pytest.raises(InvalidTransition):
        next_status(status, action)


def test_verified_is_terminal():
    assert not [k for k in TRANSITIONS if k[0] is Status.VERIFIED]


# ------------------------------------------------------------------ fixtures
@pytest.fixture()
def world(session, load_all, client):
    load_all()
    run_detection(session)
    generate_tasks(session)
    ids = {t.task_key.split(":")[1]: t.id for t in session.scalars(select(Task))}   # PO number -> task id
    return ids


def post(client, task_id, action, actor, body=None, key=None):
    headers = {"X-Actor-Id": actor}
    if key:
        headers["Idempotency-Key"] = key
    return client.post(f"/tasks/{task_id}/{action}", json=body, headers=headers)


def to_in_progress(client, tid):
    assert post(client, tid, "acknowledge", "HR-102").status_code == 200
    assert post(client, tid, "start", "HR-102").status_code == 200


def events(session, tid):
    session.expire_all()
    return [(e.seq, e.event_type, e.actor_id) for e in session.scalars(
        select(TaskEvent).where(TaskEvent.task_id == tid).order_by(TaskEvent.seq))]


# ------------------------------------------------------------------ lifecycle + audit
def test_full_lifecycle_resolves_exception_with_complete_audit_trail(client, session, world):
    tid = world["PO-501"]
    to_in_progress(client, tid)
    r = post(client, tid, "complete", "HR-102", DONE)
    assert r.status_code == 200 and r.json()["task"]["status"] == "PENDING_VERIFICATION"
    exc = session.scalars(select(ExceptionCase).where(ExceptionCase.po_number == "PO-501")).one()
    assert exc.status == "OPEN"                                   # completed != resolved

    r = post(client, tid, "verify", "HR-201")                     # store manager, different person
    assert r.status_code == 200 and r.json()["task"]["status"] == "VERIFIED"
    session.expire_all()
    exc = session.scalars(select(ExceptionCase).where(ExceptionCase.po_number == "PO-501")).one()
    assert exc.status == "RESOLVED" and exc.resolution_code == "QUANTITY_CONFIRMED_SHORT"

    assert events(session, tid) == [
        (1, "TASK_CREATED", "SYSTEM"), (2, "ASSIGNED", "SYSTEM"), (3, "ACKNOWLEDGED", "HR-102"),
        (4, "STARTED", "HR-102"), (5, "COMPLETED", "HR-102"), (6, "SENT_FOR_VERIFICATION", "SYSTEM"),
        (7, "VERIFIED", "HR-201"), (8, "EXCEPTION_RESOLVED", "SYSTEM")]


def test_task_detail_exposes_events_and_assignments(client, world):
    tid = world["PO-501"]
    to_in_progress(client, tid)
    d = client.get(f"/tasks/{tid}").json()
    assert [e["event_type"] for e in d["events"]][-2:] == ["ACKNOWLEDGED", "STARTED"]
    assert d["assignments"][0]["tier"] == "PRIMARY" and d["assignments"][0]["selection_trace"]
    exc = client.get(f"/exceptions/{d['exception_id']}").json()
    assert exc["tasks"][0]["id"] == tid


def test_list_tasks_by_assignee_and_location_with_due_time(client, world):
    mine = client.get("/tasks", params={"assignee_id": "HR-102"}).json()
    assert {t["task_key"].split(":")[1] for t in mine} == {"PO-501", "PO-506"}
    assert all(t["due_at"] and t["status"] == "ASSIGNED" for t in mine)
    assert [t["assignee_id"] for t in client.get("/tasks", params={"location": "STORE-015"}).json()] == ["HR-401"]


# ------------------------------------------------------------------ invalid transitions / authorisation
def test_client_cannot_skip_states(client, world):
    tid = world["PO-501"]
    r = post(client, tid, "start", "HR-102")                      # not acknowledged yet
    assert r.status_code == 409 and r.json()["error"] == "invalid_transition"
    post(client, tid, "acknowledge", "HR-102")
    r = post(client, tid, "complete", "HR-102", DONE)             # not started yet
    assert r.status_code == 409


def test_only_the_assignee_may_work_the_task(client, world):
    tid = world["PO-501"]
    assert post(client, tid, "acknowledge", "HR-101").status_code == 403       # off-shift colleague
    assert post(client, tid, "acknowledge", "HR-103").status_code == 403       # inactive employee
    assert post(client, tid, "acknowledge", "NOBODY").status_code == 403
    assert client.get(f"/tasks/{tid}").json()["status"] == "ASSIGNED"           # nothing changed


def test_completion_requires_valid_resolution_and_evidence(client, world):
    tid = world["PO-501"]
    to_in_progress(client, tid)
    assert post(client, tid, "complete", "HR-102", {"resolution_code": "MADE_UP", "evidence_ref": "x"}).status_code == 422
    assert post(client, tid, "complete", "HR-102", {"resolution_code": DONE["resolution_code"],
                                                    "evidence_ref": "   "}).status_code == 422
    assert client.get(f"/tasks/{tid}").json()["status"] == "IN_PROGRESS"


def test_verifier_must_be_authorised_and_different_from_worker(client, world):
    tid = world["PO-501"]
    to_in_progress(client, tid)
    post(client, tid, "complete", "HR-102", DONE)
    assert post(client, tid, "verify", "HR-102").status_code == 403        # worker verifying own work
    assert post(client, tid, "verify", "HR-401").status_code == 403        # manager of ANOTHER store
    assert post(client, tid, "verify", "HR-101").status_code == 403        # not a manager
    assert client.get(f"/tasks/{tid}").json()["status"] == "PENDING_VERIFICATION"
    assert post(client, tid, "verify", "HR-501").status_code == 200        # regional lead, region-scoped authority


def test_rejected_verification_reopens_work(client, world, session):
    tid = world["PO-501"]
    to_in_progress(client, tid)
    post(client, tid, "complete", "HR-102", DONE)
    r = post(client, tid, "reject", "HR-201", {"reason": "evidence reference does not match the GRN"})
    t = r.json()["task"]
    assert t["status"] == "IN_PROGRESS" and t["resolution_code"] is None and t["completed_by"] is None
    assert post(client, tid, "complete", "HR-102", {"resolution_code": "RECEIPT_POSTED_LATE",
                                                    "evidence_ref": "GRN-501-C"}).status_code == 200
    assert [e[1] for e in events(session, tid)].count("VERIFICATION_REJECTED") == 1


def test_block_and_unblock(client, world):
    tid = world["PO-501"]
    to_in_progress(client, tid)
    assert post(client, tid, "block", "HR-102", {"reason": "waiting for supplier call"}).json()["task"]["status"] == "BLOCKED"
    assert post(client, tid, "complete", "HR-102", DONE).status_code == 409
    assert post(client, tid, "unblock", "HR-102").json()["task"]["status"] == "IN_PROGRESS"


# ------------------------------------------------------------------ retries / idempotency
def test_repeating_a_completed_request_is_a_safe_noop(client, session, world):
    tid = world["PO-501"]
    to_in_progress(client, tid)
    first = post(client, tid, "complete", "HR-102", DONE)
    n = len(events(session, tid))
    again = post(client, tid, "complete", "HR-102", DONE)           # client retried after a timeout
    assert first.json()["replayed"] is False and again.status_code == 200 and again.json()["replayed"] is True
    assert again.json()["task"]["status"] == "PENDING_VERIFICATION"
    assert len(events(session, tid)) == n                           # no second completion event


def test_retry_with_different_payload_is_a_conflict_not_a_second_completion(client, world):
    tid = world["PO-501"]
    to_in_progress(client, tid)
    post(client, tid, "complete", "HR-102", DONE)
    r = post(client, tid, "complete", "HR-102", {**DONE, "evidence_ref": "SOMETHING-ELSE"})
    assert r.status_code == 409 and r.json()["error"] == "conflict"


def test_idempotency_key_replays_original_response(client, session, world):
    tid = world["PO-501"]
    to_in_progress(client, tid)
    a = post(client, tid, "complete", "HR-102", DONE, key="req-1")
    b = post(client, tid, "complete", "HR-102", DONE, key="req-1")
    assert a.status_code == b.status_code == 200
    assert b.headers.get("Idempotent-Replay") == "true" and b.json()["replayed"] is True
    assert a.json()["task"] == b.json()["task"]
    assert [e[1] for e in events(session, tid)].count("COMPLETED") == 1


def test_idempotency_key_cannot_be_reused_for_a_different_request(client, world):
    tid, other = world["PO-501"], world["PO-506"]
    post(client, tid, "acknowledge", "HR-102", key="k-1")
    r = post(client, other, "acknowledge", "HR-102", key="k-1")
    assert r.status_code == 422 and r.json()["error"] == "idempotency_key_reuse"
    assert client.get(f"/tasks/{other}").json()["status"] == "ASSIGNED"


def test_failed_request_does_not_burn_its_idempotency_key(client, world):
    tid = world["PO-501"]
    assert post(client, tid, "start", "HR-102", key="k-2").status_code == 409     # too early
    post(client, tid, "acknowledge", "HR-102")
    assert post(client, tid, "start", "HR-102", key="k-2").status_code == 200      # same key now works


def test_concurrent_duplicate_completion_requests_commit_exactly_once(engine, session, world, client):
    tid = world["PO-501"]
    to_in_progress(client, tid)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    results, errors = [], []

    def worker(key):
        try:
            with maker() as s:
                results.append(perform(s, "tenant-demo", tid, Action.COMPLETE, "HR-102", dict(DONE), key))
        except Exception as e:
            errors.append(e)

    for key in ("same-key", None):                                   # with and without a key
        session.execute(text("UPDATE tasks SET status='IN_PROGRESS', completed_by=NULL, resolution_code=NULL,"
                             " evidence_ref=NULL WHERE id=:i"), {"i": tid})
        session.execute(text("DELETE FROM task_events WHERE task_id=:i AND event_type IN "
                             "('COMPLETED','SENT_FOR_VERIFICATION')"), {"i": tid})
        session.execute(text("DELETE FROM idempotency_keys"))
        session.commit()
        results.clear()
        threads = [threading.Thread(target=worker, args=(key,)) for _ in range(3)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert not errors, errors
        assert sorted(r.replayed for r in results) == [False, True, True]
        assert [e[1] for e in events(session, tid)].count("COMPLETED") == 1


# ------------------------------------------------------------------ reassignment
def test_reassign_records_who_why_and_keeps_history(client, session, world):
    tid = world["PO-501"]
    r = post(client, tid, "reassign", "HR-201", {"assignee_id": "HR-201", "reason": "receiver is on a delivery"})
    assert r.status_code == 200 and r.json()["task"]["assignee_id"] == "HR-201"
    assert r.json()["task"]["status"] == "ASSIGNED"
    ev = [e for e in client.get(f"/tasks/{tid}").json()["events"] if e["event_type"] == "REASSIGNED"][0]
    assert ev["actor_id"] == "HR-201" and ev["reason"] == "receiver is on a delivery"
    assert ev["payload"] == {"from_assignee": "HR-102", "to_assignee": "HR-201"}
    hist = session.scalars(select(TaskAssignment).where(TaskAssignment.task_id == tid)
                           .order_by(TaskAssignment.assigned_at, TaskAssignment.ended_at.is_(None))).all()
    assert [(h.assignee_id, h.tier, h.assigned_by) for h in hist] == [("HR-102", "PRIMARY", "SYSTEM"),
                                                                      ("HR-201", "MANUAL", "HR-201")]
    assert post(client, tid, "acknowledge", "HR-102").status_code == 403       # previous owner lost access
    assert session.scalar(select(func.count()).select_from(ExceptionCase)) == 3   # still ONE exception per case


@pytest.mark.parametrize("target", ["HR-101", "HR-103", "HR-401", "HR-501", "GHOST"])
def test_cannot_reassign_to_ineligible_person(client, world, target):
    r = post(client, world["PO-501"], "reassign", "HR-201", {"assignee_id": target, "reason": "try"})
    assert r.status_code == 422 and r.json()["error"] == "validation_failed"
    assert client.get(f"/tasks/{world['PO-501']}").json()["assignee_id"] == "HR-102"


def test_only_managers_can_reassign(client, world):
    r = post(client, world["PO-501"], "reassign", "HR-102", {"assignee_id": "HR-201", "reason": "x"})
    assert r.status_code == 403
    assert post(client, world["PO-501"], "reassign", "HR-401",
                {"assignee_id": "HR-201", "reason": "x"}).status_code == 403     # manager of another store


def test_reassign_requires_reason_and_legal_state(client, world):
    tid = world["PO-501"]
    assert post(client, tid, "reassign", "HR-201", {"assignee_id": "HR-201", "reason": " "}).status_code == 422
    to_in_progress(client, tid)
    post(client, tid, "complete", "HR-102", DONE)
    assert post(client, tid, "reassign", "HR-201", {"assignee_id": "HR-201", "reason": "late"}).status_code == 409


def test_manual_assignment_of_unassigned_task(client, session, load_all):
    from app.ingestion.service import run_import
    from tests.conftest import fixture_bytes
    load_all()
    run_detection(session)
    roster = fixture_bytes("employees")
    run_import(session, "employees", roster.replace(b"HR-401,Rohan Iyer,ACTIVE", b"HR-401,Rohan Iyer,INACTIVE"), "e.csv")
    generate_tasks(session)
    t = client.get("/tasks", params={"needs_manual_assignment": "true"}).json()
    assert len(t) == 1 and t[0]["assignee_id"] is None and t[0]["needs_manual_assignment"] is True
    # an ineligible person still cannot be chosen manually; only an eligible one can
    assert post(client, t[0]["id"], "reassign", "HR-501", {"assignee_id": "HR-401", "reason": "x"}).status_code == 422


# ------------------------------------------------------------------ exception resolution rule + DB guards
def test_exception_resolved_only_when_every_task_is_verified(client, session, world):
    tid = world["PO-501"]
    exc = session.scalars(select(Task).where(Task.id == tid)).one().exception_id
    session.add(Task(id="extra-task", tenant_id="tenant-demo", task_key="X:SECOND", exception_id=exc,
                     task_type="SECOND_TASK", status="ASSIGNED", canonical_location_id="STORE-014",
                     assignee_id="HR-102", due_at=session.scalars(select(Task)).first().due_at,
                     created_at=session.scalars(select(Task)).first().created_at,
                     updated_at=session.scalars(select(Task)).first().updated_at))
    session.commit()
    to_in_progress(client, tid)
    post(client, tid, "complete", "HR-102", DONE)
    post(client, tid, "verify", "HR-201")
    session.expire_all()
    assert session.get(ExceptionCase, exc).status == "OPEN"        # sibling task still open


def test_database_rejects_arbitrary_status_and_unassigned_active_task(session, world):
    tid = world["PO-501"]
    with pytest.raises(IntegrityError):
        session.execute(text("UPDATE tasks SET status='DONE' WHERE id=:i"), {"i": tid})
    session.rollback()
    with pytest.raises(IntegrityError):
        session.execute(text("UPDATE tasks SET assignee_id=NULL WHERE id=:i"), {"i": tid})   # ASSIGNED w/o owner
    session.rollback()


def test_unknown_task_returns_404(client, world):
    assert post(client, "nope", "acknowledge", "HR-102").status_code == 404
    assert client.get("/tasks/nope").status_code == 404
