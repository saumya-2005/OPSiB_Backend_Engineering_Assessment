"""Shared helpers: audit events, assignment history, task snapshots, row locking."""
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Task, TaskAssignment, TaskEvent
from app.errors import NotFound
from app.routing.policy import POLICY_VERSION


def lock_task(session: Session, tenant_id: str, task_id: str) -> Task:
    """SELECT ... FOR UPDATE: concurrent actions on one task are serialised by the database."""
    task = session.scalars(select(Task).where(Task.id == task_id, Task.tenant_id == tenant_id)
                           .with_for_update()).one_or_none()
    if task is None:
        raise NotFound("task not found")
    return task


def add_event(session: Session, task: Task, event_type: str, from_status: str | None,
              to_status: str | None, actor_id: str, now: datetime, reason: str | None = None,
              payload: dict | None = None, idempotency_key: str | None = None) -> TaskEvent:
    """Append-only audit record. seq is allocated under the task row lock -> gap-free, ordered."""
    task.event_seq += 1
    ev = TaskEvent(id=str(uuid.uuid4()), task_id=task.id, seq=task.event_seq, event_type=event_type,
                   from_status=from_status, to_status=to_status, actor_id=actor_id, reason=reason,
                   payload=payload or {}, idempotency_key=idempotency_key, created_at=now)
    session.add(ev)
    return ev


def record_assignment(session: Session, task: Task, assignee_id: str | None, assigned_by: str,
                      tier: str, reason: str, trace: list, now: datetime) -> TaskAssignment:
    """Close the active assignment (history is kept) and open a new one."""
    for old in session.scalars(select(TaskAssignment).where(
            TaskAssignment.task_id == task.id, TaskAssignment.ended_at.is_(None))):
        old.ended_at = now
        old.end_reason = f"superseded by {tier} assignment to {assignee_id or 'manual queue'}"
    session.flush()
    row = TaskAssignment(id=str(uuid.uuid4()), task_id=task.id, assignee_id=assignee_id,
                         assigned_by=assigned_by, tier=tier, reason=reason, selection_trace=trace,
                         policy_version=POLICY_VERSION, assigned_at=now)
    session.add(row)
    return row


def task_view(task: Task) -> dict:
    return {
        "id": task.id, "task_key": task.task_key, "exception_id": task.exception_id,
        "task_type": task.task_type, "status": task.status,
        "canonical_location_id": task.canonical_location_id, "assignee_id": task.assignee_id,
        "needs_manual_assignment": task.assignee_id is None and task.status == "CREATED",
        "due_at": task.due_at.isoformat(), "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(), "resolution_code": task.resolution_code,
        "evidence_ref": task.evidence_ref, "completed_by": task.completed_by,
        "verified_by": task.verified_by,
    }
