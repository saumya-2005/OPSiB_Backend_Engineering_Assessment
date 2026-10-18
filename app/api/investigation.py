from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.ai.evidence import package_from_case
from app.ai.providers import get_provider
from app.ai.summary import InvestigationSummary, summarize
from app.api.schemas import ErrorOut
from app.config import Settings, get_settings
from app.db.models import ExceptionCase
from app.db.session import get_session
from app.errors import NotFound

router = APIRouter()


def provider_dep():
    return get_provider(get_settings())


class DeterministicPart(BaseModel):
    summary: str
    known: list[str]
    unknowns: list[str]
    evidence_needed: list[str]


class AiPart(BaseModel):
    status: str                     # AI_OK | FALLBACK
    source: str
    reason: str | None
    summary: InvestigationSummary | None


class InvestigationOut(BaseModel):
    exception_id: str
    deterministic: DeterministicPart   # always present; authoritative
    ai: AiPart                         # optional, validated, explanatory only


@router.get("/exceptions/{exception_id}/investigation-summary", response_model=InvestigationOut,
            responses={404: {"model": ErrorOut}})
def investigation_summary(exception_id: str, session: Session = Depends(get_session),
                          provider=Depends(provider_dep), settings: Settings = Depends(get_settings)):
    case = session.get(ExceptionCase, exception_id)
    if case is None or case.tenant_id != settings.tenant_id:
        raise NotFound("exception not found")
    res = summarize(package_from_case(case), provider, settings.ai_timeout_seconds)
    return InvestigationOut(
        exception_id=case.id,
        deterministic=DeterministicPart(summary=case.summary, known=case.known_facts,
                                        unknowns=case.unknowns, evidence_needed=case.evidence_needed),
        ai=AiPart(status=res.status, source=res.source, reason=res.reason, summary=res.summary))