from datetime import datetime

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import ErrorOut
from app.config import get_settings
from app.db.models import Task, TaskAssignment, TaskEvent
from app.db.session import get_session
from app.errors import NotFound
from app.routing.service import generate_tasks
from app.workflows.audit import task_view
from app.workflows.service import perform
from app.workflows.state_machine import Action

router = APIRouter()
ERRORS = {403: {"model": ErrorOut}, 404: {"model": ErrorOut}, 409: {"model": ErrorOut},
          422: {"model": ErrorOut}}


class TaskOut(BaseModel):
    id: str
    task_key: str
    exception_id: str
    task_type: str
    status: str
    canonical_location_id: str
    assignee_id: str | None
    needs_manual_assignment: bool
    due_at: datetime
    created_at: datetime
    updated_at: datetime
    resolution_code: str | None
    evidence_ref: str | None
    completed_by: str | None
    verified_by: str | None


class ActionOut(BaseModel):
    replayed: bool          # true = this exact request was already applied; nothing changed now
    task: TaskOut


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    seq: int
    event_type: str
    from_status: str | None
    to_status: str | None
    actor_id: str
    reason: str | None
    payload: dict
    idempotency_key: str | None
    created_at: datetime


class AssignmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    assignee_id: str | None
    assigned_by: str
    tier: str
    reason: str
    selection_trace: list
    policy_version: str
    assigned_at: datetime
    ended_at: datetime | None
    end_reason: str | None


class TaskDetailOut(TaskOut):
    events: list[EventOut]
    assignments: list[AssignmentOut]


class CompleteIn(BaseModel):
    resolution_code: str
    evidence_ref: str
    note: str | None = None


class ReasonIn(BaseModel):
    reason: str


class ReassignIn(BaseModel):
    assignee_id: str
    reason: str


class VerifyIn(BaseModel):
    note: str | None = None


class GeneratedOut(BaseModel):
    task_id: str
    case_key: str
    action: str
    status: str
    assignee_id: str | None


@router.post("/tasks/generate", response_model=list[GeneratedOut])
def generate(session: Session = Depends(get_session)):
    """Create (idempotently) one task per OPEN exception and route it. Safe to repeat;
    also retries tasks that were waiting for manual assignment."""
    return [GeneratedOut(**vars(g)) for g in generate_tasks(session)]


@router.get("/tasks", response_model=list[TaskOut])
def list_tasks(assignee_id: str | None = None, location: str | None = None, status: str | None = None,
               needs_manual_assignment: bool | None = None, session: Session = Depends(get_session)):
    q = select(Task).where(Task.tenant_id == get_settings().tenant_id)
    if assignee_id:
        q = q.where(Task.assignee_id == assignee_id)
    if location:
        q = q.where(Task.canonical_location_id == location)
    if status:
        q = q.where(Task.status == status.upper())
    if needs_manual_assignment is True:
        q = q.where(Task.assignee_id.is_(None), Task.status == "CREATED")
    return [task_view(t) for t in session.scalars(q.order_by(Task.due_at, Task.task_key))]


@router.get("/tasks/{task_id}", response_model=TaskDetailOut, responses=ERRORS)
def get_task(task_id: str, session: Session = Depends(get_session)):
    t = session.get(Task, task_id)
    if t is None or t.tenant_id != get_settings().tenant_id:
        raise NotFound("task not found")
    events = session.scalars(select(TaskEvent).where(TaskEvent.task_id == t.id).order_by(TaskEvent.seq)).all()
    assigns = session.scalars(select(TaskAssignment).where(TaskAssignment.task_id == t.id)
                              .order_by(TaskAssignment.assigned_at, TaskAssignment.ended_at.is_(None))).all()
    return {**task_view(t), "events": events, "assignments": assigns}


def _act(session, response, task_id, action, actor, body, key) -> ActionOut:
    res = perform(session, get_settings().tenant_id, task_id, action, actor, body, key)
    if res.replayed:
        response.headers["Idempotent-Replay"] = "true"
    return ActionOut(replayed=res.replayed, task=res.task)


# X-Actor-Id stands in for the authenticated user. Production: derive it (and the tenant)
# from a verified token, never from a client-supplied header.
@router.post("/tasks/{task_id}/acknowledge", response_model=ActionOut, responses=ERRORS)
def acknowledge(task_id: str, response: Response, x_actor_id: str = Header(...),
                idempotency_key: str | None = Header(None), session: Session = Depends(get_session)):
    return _act(session, response, task_id, Action.ACKNOWLEDGE, x_actor_id, {}, idempotency_key)


@router.post("/tasks/{task_id}/start", response_model=ActionOut, responses=ERRORS)
def start(task_id: str, response: Response, x_actor_id: str = Header(...),
          idempotency_key: str | None = Header(None), session: Session = Depends(get_session)):
    return _act(session, response, task_id, Action.START, x_actor_id, {}, idempotency_key)


@router.post("/tasks/{task_id}/block", response_model=ActionOut, responses=ERRORS)
def block(task_id: str, body: ReasonIn, response: Response, x_actor_id: str = Header(...),
          idempotency_key: str | None = Header(None), session: Session = Depends(get_session)):
    return _act(session, response, task_id, Action.BLOCK, x_actor_id, body.model_dump(), idempotency_key)


@router.post("/tasks/{task_id}/unblock", response_model=ActionOut, responses=ERRORS)
def unblock(task_id: str, response: Response, x_actor_id: str = Header(...),
            idempotency_key: str | None = Header(None), session: Session = Depends(get_session)):
    return _act(session, response, task_id, Action.UNBLOCK, x_actor_id, {}, idempotency_key)


@router.post("/tasks/{task_id}/complete", response_model=ActionOut, responses=ERRORS)
def complete(task_id: str, body: CompleteIn, response: Response, x_actor_id: str = Header(...),
             idempotency_key: str | None = Header(None), session: Session = Depends(get_session)):
    return _act(session, response, task_id, Action.COMPLETE, x_actor_id, body.model_dump(), idempotency_key)


@router.post("/tasks/{task_id}/verify", response_model=ActionOut, responses=ERRORS)
def verify(task_id: str, response: Response, body: VerifyIn | None = None, x_actor_id: str = Header(...),
           idempotency_key: str | None = Header(None), session: Session = Depends(get_session)):
    return _act(session, response, task_id, Action.VERIFY, x_actor_id,
                (body or VerifyIn()).model_dump(), idempotency_key)


@router.post("/tasks/{task_id}/reject", response_model=ActionOut, responses=ERRORS)
def reject(task_id: str, body: ReasonIn, response: Response, x_actor_id: str = Header(...),
           idempotency_key: str | None = Header(None), session: Session = Depends(get_session)):
    return _act(session, response, task_id, Action.REJECT, x_actor_id, body.model_dump(), idempotency_key)


@router.post("/tasks/{task_id}/reassign", response_model=ActionOut, responses=ERRORS)
def reassign(task_id: str, body: ReassignIn, response: Response, x_actor_id: str = Header(...),
             idempotency_key: str | None = Header(None), session: Session = Depends(get_session)):
    return _act(session, response, task_id, Action.REASSIGN, x_actor_id, body.model_dump(), idempotency_key)
