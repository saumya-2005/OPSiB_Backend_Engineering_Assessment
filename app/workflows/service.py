"""Task actions: validated transitions + authorisation + audit + retry safety.

Every action runs in ONE transaction:
    claim idempotency key -> lock task row -> authorise -> validate transition ->
    mutate -> write audit events -> store response for the key -> COMMIT
Any DomainError rolls everything back (including the idempotency key), so a failed
attempt leaves no trace and can be retried.

Retry safety (client timed out after the server committed "complete")
--------------------------------------------------------------------
Layer 1  Idempotency-Key header: same key + same request -> the ORIGINAL response is
         replayed, no second event. Same key + different request -> 422.
         The key row is inserted with UNIQUE(tenant_id, key) in the SAME transaction as
         the state change, so concurrent duplicates serialise on the unique index.
Layer 2  Natural idempotency without a key: the same actor repeating an action whose
         result is already in place (e.g. COMPLETE on a PENDING_VERIFICATION task with the
         same resolution) is a no-op that returns the current task. A *different*
         payload or actor gets 409/403, never a silent second completion.
"""
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import clock
from app.config import get_settings
from app.db.models import Employee, ExceptionCase, IdempotencyKey, LocationAlias, Task
from app.errors import (
    Conflict,
    DomainError,
    Forbidden,
    IdempotencyKeyReuse,
    InvalidTransitionError,
    NotFound,
    ValidationFailed,
)
from app.routing.policy import POLICIES, Candidate, rejection_reason
from app.routing.service import load_candidates
from app.workflows.audit import add_event, lock_task, record_assignment, task_view
from app.workflows.state_machine import REPLAY_OK, Action, InvalidTransition, Status, next_status

log = logging.getLogger("opsib.workflow")

RESOLUTION_CODES = frozenset({
    "QUANTITY_CONFIRMED_SHORT",     # count confirms receipts are below the PO
    "RECEIPT_POSTED_LATE",          # a missing receipt was found / posted afterwards
    "DATA_CORRECTION_REQUESTED",    # source data (PO / receipt / mapping) was wrong
    "SUPPLIER_CLAIM_RAISED",        # follow-up opened with the supplier (cause still the buyer's call)
    "NO_ACTION_REQUIRED",           # e.g. short-closed / agreed with supplier
})


@dataclass
class ActionResult:
    task: dict
    replayed: bool


# ----------------------------------------------------------------- public API
def perform(session: Session, tenant_id: str, task_id: str, action: Action, actor_id: str,
            payload: dict | None = None, idempotency_key: str | None = None) -> ActionResult:
    payload = payload or {}
    now = clock.now()
    try:
        if idempotency_key:
            replay = _claim_key(session, tenant_id, idempotency_key, task_id, action, actor_id, payload, now)
            if replay:
                session.rollback()
                return replay
        task = lock_task(session, tenant_id, task_id)
        actor = _load_actor(session, tenant_id, actor_id)
        view, replayed = _HANDLERS[action](session, tenant_id, task, actor, payload, now, idempotency_key)
        if idempotency_key and not replayed:
            row = session.scalars(select(IdempotencyKey).where(
                IdempotencyKey.tenant_id == tenant_id, IdempotencyKey.key == idempotency_key)).one()
            row.response = view
        session.commit()
        log.info("task action", extra={"task_id": task_id, "action": action.value, "actor_id": actor_id,
                                       "status": view["status"], "replayed": replayed})
        return ActionResult(view, replayed)
    except Exception:          # includes DomainError: roll back EVERYTHING, incl. the idempotency key
        session.rollback()
        raise


# ----------------------------------------------------------------- idempotency
def _fingerprint(task_id: str, action: Action, actor_id: str, payload: dict) -> str:
    body = json.dumps({"t": task_id, "a": action.value, "u": actor_id, "p": payload}, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


def _claim_key(session, tenant_id, key, task_id, action, actor_id, payload, now) -> ActionResult | None:
    fp = _fingerprint(task_id, action, actor_id, payload)
    inserted = session.execute(
        pg_insert(IdempotencyKey).values(id=str(uuid.uuid4()), tenant_id=tenant_id, key=key,
                                         fingerprint=fp, response=None, created_at=now)
        .on_conflict_do_nothing(index_elements=["tenant_id", "key"])
        .returning(IdempotencyKey.id)).scalar_one_or_none()
    if inserted:
        return None                       # first time we see this key: carry on and do the work
    existing = session.scalars(select(IdempotencyKey).where(
        IdempotencyKey.tenant_id == tenant_id, IdempotencyKey.key == key)).one()
    if existing.fingerprint != fp:
        raise IdempotencyKeyReuse("this Idempotency-Key was already used for a different request")
    if existing.response is None:
        raise Conflict("a request with this Idempotency-Key is still being processed; retry shortly")
    return ActionResult(existing.response, True)


# ----------------------------------------------------------------- authorisation helpers
def _load_actor(session: Session, tenant_id: str, actor_id: str) -> Employee:
    actor = session.scalars(select(Employee).where(
        Employee.tenant_id == tenant_id, Employee.employee_id == actor_id)).one_or_none()
    if actor is None:
        raise Forbidden(f"unknown actor '{actor_id}'")
    if actor.employment_status != "ACTIVE":
        raise Forbidden(f"actor '{actor_id}' is not an ACTIVE employee")
    return actor


def _is_manager_for(session: Session, tenant_id: str, actor: Employee, location: str) -> bool:
    if actor.role == "store_manager":
        return actor.canonical_location_id == location
    if actor.role == "regional_operations":      # region-scoped: no store, authority over the region
        region = session.scalar(select(LocationAlias.region_id).where(
            LocationAlias.tenant_id == tenant_id, LocationAlias.canonical_location_id == location).limit(1))
        return region is not None and actor.region_id == region
    return False


def _require_assignee(task: Task, actor: Employee) -> None:
    if task.assignee_id != actor.employee_id:
        raise Forbidden("only the current assignee can perform this action")


def _require_manager(session, tenant_id, task, actor) -> None:
    if not _is_manager_for(session, tenant_id, actor, task.canonical_location_id):
        raise Forbidden("requires a store manager of this location or a regional operations lead of its region")


def _transition(task: Task, action: Action) -> Status:
    try:
        return next_status(Status(task.status), action)
    except InvalidTransition as e:
        raise InvalidTransitionError(str(e)) from None


def _text(payload: dict, field: str, required: bool = True, limit: int = 255) -> str | None:
    v = payload.get(field)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise ValidationFailed(f"'{field}' is required")
        return None
    if not isinstance(v, str) or len(v) > limit:
        raise ValidationFailed(f"'{field}' must be text of at most {limit} characters")
    return v.strip()


# ----------------------------------------------------------------- handlers
def _ack(session, tenant_id, task, actor, payload, now, key):
    _require_assignee(task, actor)
    if task.status in {s.value for s in REPLAY_OK[Action.ACKNOWLEDGE]}:
        return task_view(task), True
    prev, new = task.status, _transition(task, Action.ACKNOWLEDGE)
    task.status = new.value
    task.updated_at = now
    add_event(session, task, "ACKNOWLEDGED", prev, new.value, actor.employee_id, now, idempotency_key=key)
    return task_view(task), False


def _start(session, tenant_id, task, actor, payload, now, key):
    _require_assignee(task, actor)
    if task.status in {s.value for s in REPLAY_OK[Action.START]}:
        return task_view(task), True
    prev, new = task.status, _transition(task, Action.START)
    task.status = new.value
    task.updated_at = now
    add_event(session, task, "STARTED", prev, new.value, actor.employee_id, now, idempotency_key=key)
    return task_view(task), False


def _block(session, tenant_id, task, actor, payload, now, key):
    _require_assignee(task, actor)
    reason = _text(payload, "reason")
    if task.status in {s.value for s in REPLAY_OK[Action.BLOCK]}:
        return task_view(task), True
    prev, new = task.status, _transition(task, Action.BLOCK)
    task.status = new.value
    task.updated_at = now
    add_event(session, task, "BLOCKED", prev, new.value, actor.employee_id, now, reason, idempotency_key=key)
    return task_view(task), False


def _unblock(session, tenant_id, task, actor, payload, now, key):
    _require_assignee(task, actor)
    prev, new = task.status, _transition(task, Action.UNBLOCK)
    task.status = new.value
    task.updated_at = now
    add_event(session, task, "UNBLOCKED", prev, new.value, actor.employee_id, now,
              _text(payload, "reason", required=False), idempotency_key=key)
    return task_view(task), False


def _complete(session, tenant_id, task, actor, payload, now, key):
    _require_assignee(task, actor)
    code = _text(payload, "resolution_code", limit=48)
    if code not in RESOLUTION_CODES:
        raise ValidationFailed(f"'resolution_code' must be one of {sorted(RESOLUTION_CODES)}")
    evidence = _text(payload, "evidence_ref")
    note = _text(payload, "note", required=False, limit=1000)

    if task.status in {s.value for s in REPLAY_OK[Action.COMPLETE]}:       # retry of a committed completion
        if task.completed_by == actor.employee_id and task.resolution_code == code \
                and task.evidence_ref == evidence:
            return task_view(task), True
        raise Conflict("task was already completed with a different resolution; "
                       "a completed task cannot be completed again")

    prev = task.status
    mid = _transition(task, Action.COMPLETE)                       # IN_PROGRESS -> COMPLETED
    task.status = mid.value
    task.completed_by, task.completed_at = actor.employee_id, now
    task.resolution_code, task.evidence_ref = code, evidence
    add_event(session, task, "COMPLETED", prev, mid.value, actor.employee_id, now, note,
              {"resolution_code": code, "evidence_ref": evidence}, key)
    final = _transition(task, Action.SUBMIT_FOR_VERIFICATION)      # COMPLETED -> PENDING_VERIFICATION
    task.status = final.value
    task.updated_at = now
    add_event(session, task, "SENT_FOR_VERIFICATION", mid.value, final.value, "SYSTEM", now,
              "completed work must be verified by a different authorised person")
    return task_view(task), False


def _verifier_rules(session, tenant_id, task, actor) -> None:
    _require_manager(session, tenant_id, task, actor)
    if actor.employee_id in (task.completed_by, task.assignee_id):
        raise Forbidden("the verifier must be a different person from the one who did the work")


def _verify(session, tenant_id, task, actor, payload, now, key):
    _verifier_rules(session, tenant_id, task, actor)
    if task.status in {s.value for s in REPLAY_OK[Action.VERIFY]} and task.verified_by == actor.employee_id:
        return task_view(task), True
    prev, new = task.status, _transition(task, Action.VERIFY)          # 409 unless PENDING_VERIFICATION
    if not task.resolution_code or not task.evidence_ref:             # verification criteria
        raise ValidationFailed("task has no resolution code / evidence reference to verify")
    task.status = new.value
    task.verified_by, task.verified_at, task.updated_at = actor.employee_id, now, now
    add_event(session, task, "VERIFIED", prev, new.value, actor.employee_id, now,
              _text(payload, "note", required=False, limit=1000), idempotency_key=key)

    # The exception is resolved only when EVERY task of that exception is VERIFIED.
    exc = session.scalars(select(ExceptionCase).where(ExceptionCase.id == task.exception_id)
                          .with_for_update()).one()
    open_siblings = session.scalars(select(Task.id).where(
        Task.exception_id == exc.id, Task.id != task.id, Task.status != "VERIFIED")).all()
    if not open_siblings and exc.status == "OPEN":
        exc.status, exc.resolution_code, exc.resolved_at = "RESOLVED", task.resolution_code, now
        add_event(session, task, "EXCEPTION_RESOLVED", new.value, new.value, "SYSTEM", now,
                  "all tasks verified", {"exception_id": exc.id, "resolution_code": task.resolution_code})
    return task_view(task), False


def _reject(session, tenant_id, task, actor, payload, now, key):
    _verifier_rules(session, tenant_id, task, actor)
    reason = _text(payload, "reason")
    prev, new = task.status, _transition(task, Action.REJECT)
    previous = {"resolution_code": task.resolution_code, "evidence_ref": task.evidence_ref,
                "completed_by": task.completed_by}
    task.status = new.value
    task.resolution_code = task.evidence_ref = task.completed_by = task.completed_at = None
    task.updated_at = now
    add_event(session, task, "VERIFICATION_REJECTED", prev, new.value, actor.employee_id, now, reason,
              {"rejected_completion": previous}, key)
    return task_view(task), False


def _reassign(session, tenant_id, task, actor, payload, now, key):
    _require_manager(session, tenant_id, task, actor)
    new_assignee = _text(payload, "assignee_id", limit=64)
    reason = _text(payload, "reason")
    prev_status, prev_assignee = task.status, task.assignee_id
    new_status = _transition(task, Action.REASSIGN)               # 409 if the state forbids it
    if new_assignee == prev_assignee:
        raise ValidationFailed("task is already assigned to that person")

    exc = session.get(ExceptionCase, task.exception_id)
    policy = POLICIES[exc.exception_type]
    cands = [c for c in load_candidates(session, tenant_id, task.canonical_location_id,
                                        policy.all_roles, now) if c.employee_id == new_assignee]
    if not cands:
        emp = session.scalars(select(Employee).where(Employee.tenant_id == tenant_id,
                                                     Employee.employee_id == new_assignee)).one_or_none()
        why = ("unknown employee" if emp is None else
               f"employment status {emp.employment_status}" if emp.employment_status != "ACTIVE" else
               f"role '{emp.role}' / location is not eligible for this task")
        raise ValidationFailed(f"'{new_assignee}' cannot be assigned: {why}")
    cand: Candidate = cands[0]
    why_not = rejection_reason(cand, task.canonical_location_id, now,
                               get_settings().availability_max_age_minutes)
    if why_not:                                    # same safety invariant as automatic routing
        raise ValidationFailed(f"'{new_assignee}' cannot be assigned: {why_not}")

    trace = [{"employee_id": cand.employee_id, "role": cand.role, "tier": "MANUAL",
              "eligible": True, "reason": "eligible"}]
    record_assignment(session, task, new_assignee, actor.employee_id, "MANUAL",
                      f"reassigned by {actor.employee_id}: {reason}", trace, now)
    task.assignee_id, task.status, task.updated_at = new_assignee, new_status.value, now
    add_event(session, task, "REASSIGNED", prev_status, new_status.value, actor.employee_id, now, reason,
              {"from_assignee": prev_assignee, "to_assignee": new_assignee}, key)
    return task_view(task), False


_HANDLERS = {
    Action.ACKNOWLEDGE: _ack, Action.START: _start, Action.BLOCK: _block, Action.UNBLOCK: _unblock,
    Action.COMPLETE: _complete, Action.VERIFY: _verify, Action.REJECT: _reject, Action.REASSIGN: _reassign,
}