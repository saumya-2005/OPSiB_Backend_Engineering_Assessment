from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import ErrorOut
from app.clock import now
from app.config import get_settings
from app.db.models import ExceptionCase, Task
from app.db.session import get_session
from app.detection.service import run_detection

router = APIRouter()


class EvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    role: str
    record_type: str
    source_system: str
    source_record_id: str
    import_batch_id: str
    qty_base: Decimal | None
    note: str | None


class ExceptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    case_key: str
    exception_type: str
    status: str
    rule_version: str
    mapping_version: str
    detected_at: datetime
    last_evaluated_at: datetime
    revision: int
    po_number: str
    po_line_number: int
    supplier_code: str
    canonical_product_id: str
    canonical_location_id: str
    expected_qty: Decimal
    received_qty: Decimal
    variance_qty: Decimal
    unit: str
    expected_receipt_by: datetime
    summary: str
    known_facts: list[str]
    unknowns: list[str]
    evidence_needed: list[str]
    close_reason: str | None
    resolution_code: str | None
    resolved_at: datetime | None


class TaskBrief(BaseModel):
    id: str
    status: str
    assignee_id: str | None
    due_at: datetime


class ExceptionDetailOut(ExceptionOut):
    evidence: list[EvidenceOut]
    tasks: list[TaskBrief] = []


class LineReportOut(BaseModel):
    po_number: str
    line_number: int
    outcome: str
    reason: str
    expected: Decimal | None
    received: Decimal | None
    variance: Decimal | None
    exception_id: str | None
    case_action: str | None


class DetectionRunOut(BaseModel):
    evaluated_at: datetime
    rule_version: str
    lines: list[LineReportOut]


@router.post("/detection/run", response_model=DetectionRunOut)
def detection_run(session: Session = Depends(get_session)):
    """Re-evaluate every PO line at the frozen evaluation time. Safe to call repeatedly."""
    lines = run_detection(session)
    return DetectionRunOut(evaluated_at=now(), rule_version=get_settings().rule_version,
                           lines=[LineReportOut(**vars(x)) for x in lines])


@router.get("/exceptions", response_model=list[ExceptionOut])
def list_exceptions(status: str | None = None, location: str | None = None,
                    session: Session = Depends(get_session)):
    q = select(ExceptionCase).where(ExceptionCase.tenant_id == get_settings().tenant_id)
    if status:
        q = q.where(ExceptionCase.status == status.upper())
    if location:
        q = q.where(ExceptionCase.canonical_location_id == location)
    return session.scalars(q.order_by(ExceptionCase.po_number, ExceptionCase.po_line_number)).all()


@router.get("/exceptions/{exception_id}", response_model=ExceptionDetailOut,
            responses={404: {"model": ErrorOut}})
def get_exception(exception_id: str, session: Session = Depends(get_session)):
    case = session.get(ExceptionCase, exception_id)
    if case is None or case.tenant_id != get_settings().tenant_id:
        raise HTTPException(404, detail="exception not found")
    out = ExceptionDetailOut.model_validate(case)
    tasks = session.scalars(select(Task).where(Task.exception_id == case.id).order_by(Task.created_at)).all()
    out.tasks = [TaskBrief(id=x.id, status=x.status, assignee_id=x.assignee_id, due_at=x.due_at) for x in tasks]
    return out
