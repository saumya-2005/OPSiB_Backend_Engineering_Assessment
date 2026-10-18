"""Generic, idempotent CSV loader.

Transaction boundaries
----------------------
1. Register the batch (status PROCESSING) and COMMIT -> a crash is still visible.
2. Process all rows in ONE transaction:
     - bad rows are recorded as RowIssue (not exceptions) -> partial success is normal
     - an unexpected error rolls back every fact from this file and marks the batch FAILED
3. Mark the batch finished and COMMIT.

Idempotency / concurrency
-------------------------
Each row is written with
    INSERT ... ON CONFLICT (tenant_id, source_system, source_record_id) DO NOTHING
so the *database* decides who wins. Two workers (or one retried request) importing
the same file concurrently: the second INSERT waits on the unique index, then does
nothing. We then compare content_hash with the stored row:
    same hash      -> DUPLICATE (counted, no new fact)
    different hash -> CONFLICT  (old row kept, RowIssue for review)
"""
import csv
import hashlib
import io
import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import clock
from app.config import get_settings
from app.db.models import ImportBatch, RowIssue
from app.ingestion.datasets import REGISTRY, Built, DatasetSpec
from app.mapping.resolver import Resolver

log = logging.getLogger("opsib.ingestion")


class UnknownDatasetError(Exception):
    pass


class InvalidFileError(Exception):
    pass


def _norm(v: Any) -> str:
    """Stable text form of a value so the same business content always hashes the same."""
    if isinstance(v, datetime):
        # compare instants, not spellings: 10:00+05:30 == 04:30Z
        return v.astimezone(timezone.utc).isoformat()
    if isinstance(v, Decimal):
        return format(v.normalize(), "f")
    return str(v)


def content_hash(values: dict[str, Any], fields: tuple[str, ...]) -> str:
    payload = json.dumps({f: _norm(values[f]) for f in fields}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _decode(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise InvalidFileError(f"file is not valid UTF-8: {e}") from e


def _changed_fields(existing: Any, values: dict[str, Any], fields: tuple[str, ...]) -> str:
    diffs = [f"{f}: {_norm(getattr(existing, f))} -> {_norm(values[f])}"
             for f in fields if _norm(getattr(existing, f)) != _norm(values[f])]
    return "; ".join(diffs) or "content differs"


def run_import(session: Session, dataset: str, content: bytes, filename: str | None,
               tenant_id: str | None = None) -> ImportBatch:
    spec = REGISTRY.get(dataset)
    if spec is None:
        raise UnknownDatasetError(dataset)
    settings = get_settings()
    tenant_id = tenant_id or settings.tenant_id
    text = _decode(content)
    reader = csv.DictReader(io.StringIO(text))

    batch = ImportBatch(
        id=str(uuid.uuid4()), tenant_id=tenant_id, dataset=dataset, filename=filename,
        file_sha256=hashlib.sha256(content).hexdigest(), status="PROCESSING",
        mapping_version=settings.mapping_version, started_at=clock.now(),
    )
    session.add(batch)

    # Schema field names equal the CSV header names by design.
    missing = [c for c in spec.required_columns if c not in (reader.fieldnames or [])]
    if missing:
        batch.status = "FAILED"
        batch.finished_at = clock.now()
        session.add(RowIssue(
            tenant_id=tenant_id, batch_id=batch.id, dataset=dataset, row_number=0,
            source_record_id=None, kind="REJECTED", reason_code="MISSING_COLUMNS",
            detail=f"file is missing required columns: {', '.join(missing)}", raw={}))
        session.commit()
        raise InvalidFileError(f"missing required columns: {', '.join(missing)}")

    session.commit()  # (1) batch is now visible as PROCESSING
    try:
        _process_rows(session, spec, reader, batch, tenant_id)
        batch.finished_at = clock.now()
        problems = batch.rejected_rows + batch.conflict_rows + _unmapped_count(session, batch.id)
        batch.status = "COMPLETED_WITH_ISSUES" if problems else "COMPLETED"
        session.commit()  # (3)
    except Exception:
        session.rollback()
        session.execute(update(ImportBatch).where(ImportBatch.id == batch.id)
                        .values(status="FAILED", finished_at=clock.now()))
        session.commit()
        log.exception("import failed", extra={"batch_id": batch.id, "dataset": dataset})
        raise
    session.refresh(batch)
    log.info("import finished", extra={
        "batch_id": batch.id, "dataset": dataset, "status": batch.status, "total": batch.total_rows,
        "accepted": batch.accepted_rows, "rejected": batch.rejected_rows,
        "duplicate": batch.duplicate_rows, "conflict": batch.conflict_rows})
    return batch


def _unmapped_count(session: Session, batch_id: str) -> int:
    return len(session.scalars(select(RowIssue.id).where(
        RowIssue.batch_id == batch_id, RowIssue.kind == "UNMAPPED")).all())


def _issue(session, batch, tenant_id, spec, n, sid, kind, code, detail, raw):
    session.add(RowIssue(tenant_id=tenant_id, batch_id=batch.id, dataset=spec.name,
                         row_number=n, source_record_id=sid, kind=kind,
                         reason_code=code, detail=detail, raw=raw))


def _process_rows(session: Session, spec: DatasetSpec, reader, batch: ImportBatch, tenant_id: str):
    resolver = Resolver(session, tenant_id)
    model = spec.model
    model_cols = {c.name for c in model.__table__.columns}
    version = get_settings().mapping_version

    for n, raw in enumerate(reader, start=1):
        batch.total_rows += 1
        sid = (raw.get(spec.id_field) or "").strip() or None
        raw_clean = {k: v for k, v in raw.items() if k is not None}

        # --- 1. typed validation -> REJECTED (visible, with reason) ---
        try:
            row = spec.schema.model_validate(raw_clean)
        except ValidationError as e:
            detail = "; ".join(f"{'.'.join(map(str, err['loc'])) or 'row'}: {err['msg']}"
                               for err in e.errors())
            batch.rejected_rows += 1
            _issue(session, batch, tenant_id, spec, n, sid, "REJECTED", "VALIDATION_ERROR", detail, raw_clean)
            continue

        # --- 2. identity mapping + UOM normalisation ---
        built: Built = spec.build(row, resolver)
        values = dict(built.values)
        values["tenant_id"] = tenant_id
        values["import_batch_id"] = batch.id
        if "mapping_version" in model_cols:
            values["mapping_version"] = version
        h = content_hash(values, spec.fact_fields)
        values["content_hash"] = h

        # --- 3. DB-decided idempotent insert ---
        stmt = (pg_insert(model).values(id=str(uuid.uuid4()), **values)
                .on_conflict_do_nothing(index_elements=list(spec.identity_cols))
                .returning(model.id))
        inserted = session.execute(stmt).scalar_one_or_none()

        if inserted is not None:
            batch.accepted_rows += 1
            if built.issue:
                _issue(session, batch, tenant_id, spec, n, sid, "UNMAPPED",
                       built.issue[0], built.issue[1], raw_clean)
            continue

        # Already exists: identical repeat, late re-mapping, roster update, or conflict.
        existing = session.scalars(select(model).where(
            *[getattr(model, c) == values[c] for c in spec.identity_cols])).one()
        session.refresh(existing)  # see rows committed by concurrent workers

        if existing.content_hash == h:
            batch.duplicate_rows += 1
            if (built.issue is None and "mapping_status" in model_cols
                    and existing.mapping_status == "UNMAPPED"):
                _apply_late_mapping(session, existing, spec, values, version, tenant_id, sid)
                batch.updated_rows += 1
        elif spec.on_change == "update":
            for f in (*spec.fact_fields, *spec.derived_fields, "content_hash", "import_batch_id"):
                setattr(existing, f, values[f])
            batch.updated_rows += 1
            if built.issue:
                _issue(session, batch, tenant_id, spec, n, sid, "UNMAPPED",
                       built.issue[0], built.issue[1], raw_clean)
        else:
            batch.conflict_rows += 1
            _issue(session, batch, tenant_id, spec, n, sid, "CONFLICT", "CONFLICTING_DUPLICATE_ID",
                   "same source id arrived with different content; existing row kept. "
                   + _changed_fields(existing, values, spec.fact_fields), raw_clean)


def _apply_late_mapping(session, existing, spec, values, version, tenant_id, sid):
    """An alias was added after the fact was first imported: fill the DERIVED columns only.
    Source facts are untouched, and the earlier UNMAPPED data-quality issue is closed."""
    for f in spec.derived_fields:
        setattr(existing, f, values[f])
    if hasattr(existing, "mapping_version"):
        existing.mapping_version = version
    for issue in session.scalars(select(RowIssue).where(
            RowIssue.tenant_id == tenant_id, RowIssue.dataset == spec.name,
            RowIssue.source_record_id == sid, RowIssue.kind == "UNMAPPED",
            RowIssue.resolved.is_(False))):
        issue.resolved = True
