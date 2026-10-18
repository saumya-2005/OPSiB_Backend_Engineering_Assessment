"""AI boundary: what goes IN to a model, what may come OUT, and how it is validated.

Principle: a deterministic detector produces the finding; AI may only EXPLAIN it.

AI may      : explain the evidence, point out missing context, suggest investigation steps
AI may NOT  : calculate authoritative quantities, invent source records, decide who is
              allowed to do something, write to the ERP, or mark anything verified

How that is enforced in code (not by prompting alone):
  1. Input is a small EvidencePackage (ids + pre-computed numbers). No employee data.
  2. Output must parse into InvestigationSummary. extra="forbid" -> there is simply no
     field for quantities, status, verified flags or assignees; extra fields are rejected.
  3. Every cited record ref must exist in the package we sent.
  4. Any number appearing in the text must already appear in the package (no invented
     quantities); assertive cause claims are rejected (the data cannot prove cause).
  5. ANY failure (timeout, bad JSON, schema error, citation error, provider down) falls
     back to the deterministic explanation. The application never depends on the model.
"""
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger("opsib.ai")


class EvidenceRecord(BaseModel):
    ref: str                       # e.g. "RECEIPT_COUNTED:WMS-GRN501-A-L1"  <- what the model may cite
    role: str
    source_system: str
    source_record_id: str
    qty_base: str | None = None
    note: str | None = None


class EvidencePackage(BaseModel):
    case_key: str
    exception_type: str
    facts: dict[str, str]          # already computed by the rule engine; the model never recomputes
    known: list[str]
    unknowns: list[str]
    evidence_needed: list[str]
    evidence: list[EvidenceRecord]

    def refs(self) -> set[str]:
        return {e.ref for e in self.evidence}


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=300)
    cites: list[str] = Field(default_factory=list, max_length=10)


class InvestigationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")     # no 'status', 'verified', 'variance', 'assignee' ...
    explanation: str = Field(max_length=1200)
    missing_context: list[str] = Field(default_factory=list, max_length=8)
    suggested_steps: list[Step] = Field(default_factory=list, max_length=8)
    cited_refs: list[str] = Field(default_factory=list, max_length=20)


class SummaryRejected(Exception):
    """Model output failed validation."""


class ProviderUnavailable(Exception):
    """No provider configured / reachable."""


class InvestigationProvider(Protocol):
    name: str

    def summarize(self, package: EvidencePackage) -> "str | dict": ...


_NUM = re.compile(r"\d+(?:\.\d+)?")
_ASSERTIVE_CAUSE = ("supplier is at fault", "supplier was at fault", "supplier failed to", "short-shipped",
                    "was lost in transit", "lost by the carrier", "was stolen", "caused by the supplier")


def validate_summary(raw: "str | dict", package: EvidencePackage) -> InvestigationSummary:
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as e:
        raise SummaryRejected(f"not valid JSON: {e.msg}") from e
    try:
        summary = InvestigationSummary.model_validate(data)
    except ValidationError as e:
        raise SummaryRejected(f"schema violation: {e.errors()[0]['type']} at {e.errors()[0]['loc']}") from e

    cited = set(summary.cited_refs) | {c for s in summary.suggested_steps for c in s.cites}
    unknown = cited - package.refs()
    if unknown:
        raise SummaryRejected(f"cites records that were not in the evidence: {sorted(unknown)}")

    texts = [summary.explanation, *summary.missing_context, *(s.text for s in summary.suggested_steps)]
    allowed = set(_NUM.findall(package.model_dump_json()))
    invented = {n for t in texts for n in _NUM.findall(t)} - allowed
    if invented:
        raise SummaryRejected(f"introduces numbers not present in the evidence: {sorted(invented)}")
    blob = " ".join(texts).lower()
    for phrase in _ASSERTIVE_CAUSE:
        if phrase in blob:
            raise SummaryRejected(f"asserts a cause the data cannot prove ('{phrase}')")
    return summary


@dataclass
class SummaryResult:
    status: str                    # AI_OK | FALLBACK
    source: str                    # provider name
    summary: InvestigationSummary | None
    reason: str | None             # why we fell back


def summarize(package: EvidencePackage, provider: InvestigationProvider,
              timeout_s: float) -> SummaryResult:
    """Never raises. Returns AI output only if it passed every check."""
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        raw = pool.submit(provider.summarize, package).result(timeout=timeout_s)
        return SummaryResult("AI_OK", provider.name, validate_summary(raw, package), None)
    except FutureTimeout:
        reason = f"provider timed out after {timeout_s}s"
    except ProviderUnavailable as e:
        reason = str(e)
    except SummaryRejected as e:
        reason = f"output rejected: {e}"
    except Exception as e:                       # provider/network bugs must not break the app
        reason = f"provider error: {type(e).__name__}"
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    log.warning("ai summary fell back", extra={"provider": provider.name, "reason": reason})
    return SummaryResult("FALLBACK", provider.name, None, reason)