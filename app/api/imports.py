from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.schemas import DataQualityOut, ErrorOut, ImportOut, RowIssueOut
from app.config import get_settings
from app.db.models import ImportBatch, RowIssue
from app.db.session import get_session
from app.ingestion.service import InvalidFileError, UnknownDatasetError, run_import

router = APIRouter()


@router.post("/imports/{dataset}", response_model=ImportOut,
             responses={404: {"model": ErrorOut}, 422: {"model": ErrorOut}})
async def create_import(dataset: str, response: Response, file: UploadFile = File(...),
                        session: Session = Depends(get_session)):
    """Validate and load one CSV.

    201 = every row clean.  207 = loaded, but some rows were rejected / conflicting /
    unmapped (see `issues`).  Partial failure is never reported as plain success.
    """
    content = await file.read()
    try:
        batch = run_import(session, dataset, content, file.filename)
    except UnknownDatasetError:
        raise HTTPException(404, detail=f"unknown dataset '{dataset}'")
    except InvalidFileError as e:
        raise HTTPException(422, detail=str(e))
    response.status_code = 201 if batch.status == "COMPLETED" else 207
    return _out(session, batch)


@router.get("/imports/{import_id}", response_model=ImportOut, responses={404: {"model": ErrorOut}})
def get_import(import_id: str, session: Session = Depends(get_session)):
    batch = session.get(ImportBatch, import_id)
    if batch is None:
        raise HTTPException(404, detail="import not found")
    return _out(session, batch)


@router.get("/data-quality", response_model=DataQualityOut)
def data_quality(kind: str | None = None, dataset: str | None = None, limit: int = 200,
                 session: Session = Depends(get_session)):
    """Everything that needs a human: rejected rows, conflicting ids, missing mappings."""
    tenant = get_settings().tenant_id
    base = [RowIssue.tenant_id == tenant, RowIssue.resolved.is_(False)]
    if kind:
        base.append(RowIssue.kind == kind.upper())
    if dataset:
        base.append(RowIssue.dataset == dataset)
    rows = session.scalars(select(RowIssue).where(*base)
                           .order_by(RowIssue.dataset, RowIssue.row_number).limit(min(limit, 1000))).all()
    counts = dict(session.execute(select(RowIssue.kind, func.count()).where(*base)
                                  .group_by(RowIssue.kind)).all())
    return DataQualityOut(open_issue_count=sum(counts.values()), by_kind=counts,
                          issues=[RowIssueOut.model_validate(r) for r in rows])


def _out(session: Session, batch: ImportBatch) -> ImportOut:
    out = ImportOut.model_validate(batch)
    issues = session.scalars(select(RowIssue).where(RowIssue.batch_id == batch.id)
                             .order_by(RowIssue.row_number).limit(500)).all()
    out.issues = [RowIssueOut.model_validate(i) for i in issues]
    return out
