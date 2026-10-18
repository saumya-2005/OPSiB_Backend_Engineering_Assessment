"""Turn OPEN exceptions into tasks and pick assignees (DB layer around policy.py).

Two workers must not create two tasks for one issue:
  UNIQUE(tenant_id, task_key) + INSERT ... ON CONFLICT DO NOTHING. Only the worker whose
  INSERT actually inserted a row writes the TASK_CREATED event and the first assignment.
The other worker sees no row returned and just reads the existing task.
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import clock
from app.config import get_settings
from app.db.models import Employee, ExceptionCase, Task, WorkforceSnapshot
from app.routing.policy import POLICIES, Candidate, Decision, select_assignee
from app.workflows.audit import add_event, lock_task, record_assignment

log = logging.getLogger("opsib.routing")
TASK_TYPE = "INVESTIGATE_RECEIPT_SHORTFALL"
SYSTEM = "SYSTEM"


@dataclass
class GeneratedTask:
    task_id: str
    case_key: str
    action: str          # CREATED | ASSIGNED_LATE | STILL_UNASSIGNED | UNCHANGED
    status: str
    assignee_id: str | None


def load_candidates(session: Session, tenant_id: str, location: str, roles: tuple[str, ...],
                    now: datetime) -> list[Candidate]:
    """Employees with a relevant role at this CANONICAL location + their latest snapshot at/before now."""
    out = []
    emps = session.scalars(select(Employee).where(
        Employee.tenant_id == tenant_id, Employee.canonical_location_id == location,
        Employee.role.in_(roles)).order_by(Employee.employee_id))
    for e in emps:
        snap = session.scalars(select(WorkforceSnapshot).where(
            WorkforceSnapshot.tenant_id == tenant_id, WorkforceSnapshot.employee_id == e.employee_id,
            WorkforceSnapshot.observed_at <= now).order_by(WorkforceSnapshot.observed_at.desc())
            .limit(1)).one_or_none()
        out.append(Candidate(e.employee_id, e.role, e.employment_status, e.canonical_location_id,
                             snap.availability_status if snap else None, snap.shift_start if snap else None,
                             snap.shift_end if snap else None, snap.observed_at if snap else None))
    return out


def _decide(session: Session, tenant_id: str, exc: ExceptionCase, now: datetime) -> Decision:
    policy = POLICIES[exc.exception_type]
    cands = load_candidates(session, tenant_id, exc.canonical_location_id, policy.all_roles, now)
    return select_assignee(cands, policy, exc.canonical_location_id, now,
                           get_settings().availability_max_age_minutes)


def _apply_decision(session: Session, task: Task, d: Decision, now: datetime) -> None:
    record_assignment(session, task, d.assignee_id, SYSTEM, d.tier, d.reason, d.trace, now)
    if d.assignee_id:
        task.assignee_id = d.assignee_id
        task.status = "ASSIGNED"
        add_event(session, task, "ASSIGNED", "CREATED", "ASSIGNED", SYSTEM, now, d.reason,
                  {"assignee_id": d.assignee_id, "tier": d.tier})
    else:
        add_event(session, task, "NO_ELIGIBLE_ASSIGNEE", "CREATED", "CREATED", SYSTEM, now, d.reason,
                  {"tier": d.tier})
    task.updated_at = now


def generate_tasks(session: Session, tenant_id: str | None = None) -> list[GeneratedTask]:
    settings = get_settings()
    tenant_id = tenant_id or settings.tenant_id
    now = clock.now()
    out: list[GeneratedTask] = []

    cases = session.scalars(select(ExceptionCase).where(
        ExceptionCase.tenant_id == tenant_id, ExceptionCase.status == "OPEN")
        .order_by(ExceptionCase.po_number, ExceptionCase.po_line_number)).all()
    for exc in cases:
        key = f"{exc.case_key}:{TASK_TYPE}"
        new_id = str(uuid.uuid4())
        inserted = session.execute(
            pg_insert(Task).values(
                id=new_id, tenant_id=tenant_id, task_key=key, exception_id=exc.id, task_type=TASK_TYPE,
                status="CREATED", canonical_location_id=exc.canonical_location_id, assignee_id=None,
                due_at=now + timedelta(hours=settings.task_due_hours), created_at=now, updated_at=now,
                event_seq=0)
            .on_conflict_do_nothing(index_elements=["tenant_id", "task_key"])
            .returning(Task.id)).scalar_one_or_none()

        if inserted:                                   # we are the one creator
            task = session.get(Task, new_id)
            add_event(session, task, "TASK_CREATED", None, "CREATED", SYSTEM, now,
                      f"created for exception {exc.case_key}", {"exception_id": exc.id})
            _apply_decision(session, task, _decide(session, tenant_id, exc, now), now)
            action = "CREATED"
        else:
            task = session.scalars(select(Task).where(Task.tenant_id == tenant_id,
                                                      Task.task_key == key)).one()
            task = lock_task(session, tenant_id, task.id)
            if task.status == "CREATED" and task.assignee_id is None:     # retry the unassigned queue
                d = _decide(session, tenant_id, exc, now)
                if d.assignee_id:
                    _apply_decision(session, task, d, now)
                    action = "ASSIGNED_LATE"
                else:
                    action = "STILL_UNASSIGNED"
            else:
                action = "UNCHANGED"
        out.append(GeneratedTask(task.id, exc.case_key, action, task.status, task.assignee_id))
        log.info("task generated", extra={"task_id": task.id, "case_key": exc.case_key, "action": action})
    session.commit()
    return out
