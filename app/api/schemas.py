from datetime import datetime

from pydantic import BaseModel, ConfigDict


class RowIssueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    batch_id: str
    dataset: str
    row_number: int
    source_record_id: str | None
    kind: str            # REJECTED | CONFLICT | UNMAPPED
    reason_code: str
    detail: str
    raw: dict
    resolved: bool


class ImportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    dataset: str
    filename: str | None
    file_sha256: str
    status: str          # PROCESSING | COMPLETED | COMPLETED_WITH_ISSUES | FAILED
    total_rows: int
    accepted_rows: int
    rejected_rows: int
    duplicate_rows: int
    conflict_rows: int
    updated_rows: int
    mapping_version: str
    started_at: datetime
    finished_at: datetime | None
    issues: list[RowIssueOut] = []


class DataQualityOut(BaseModel):
    open_issue_count: int
    by_kind: dict[str, int]
    issues: list[RowIssueOut]


class ErrorOut(BaseModel):
    """Uniform error body for every non-2xx response."""
    error: str
    detail: str
