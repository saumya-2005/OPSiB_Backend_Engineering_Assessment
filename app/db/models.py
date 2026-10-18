"""Database schema (Step 1: reference data + ingestion + source facts).

Design rules
------------
* RAW input vs BUSINESS state are separate: every imported row keeps its source
  identity + original values; canonical ids are *derived* columns that can be
  NULL when mapping is missing (that is a data-quality issue, not a guess).
* Idempotency is enforced by the DATABASE, not by "if exists" code alone:
  UNIQUE(tenant_id, source_system, source_record_id) on every source fact.
* content_hash = hash of business-relevant fields. Same id + same hash = harmless
  repeat. Same id + different hash = CONFLICT -> goes to review, never overwrites.
"""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def _uuid() -> str:
    return str(uuid.uuid4())


# --------------------------------------------------------------------------
# Reference / crosswalk data (loaded from product_aliases.csv, location_aliases.csv)
# --------------------------------------------------------------------------
class ProductAlias(Base):
    """(source system, external item id) -> canonical product + UOM conversion.

    One external id maps to exactly one canonical product (enforced by UNIQUE).
    Same canonical product can have many aliases (ERP id, WMS id ...).
    """
    __tablename__ = "product_aliases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_system", "external_item_id", name="uq_product_alias"),
        CheckConstraint("to_base_factor > 0", name="ck_factor_positive"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64))
    source_system: Mapped[str] = mapped_column(String(16))
    external_item_id: Mapped[str] = mapped_column(String(64))
    canonical_product_id: Mapped[str] = mapped_column(String(64))
    source_uom: Mapped[str] = mapped_column(String(16))
    base_uom: Mapped[str] = mapped_column(String(16))
    to_base_factor: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    mapping_version: Mapped[str] = mapped_column(String(32))
    content_hash: Mapped[str] = mapped_column(String(64))
    import_batch_id: Mapped[str] = mapped_column(ForeignKey("import_batches.id"))


class LocationAlias(Base):
    __tablename__ = "location_aliases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_system", "external_location_id", name="uq_location_alias"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64))
    source_system: Mapped[str] = mapped_column(String(16))
    external_location_id: Mapped[str] = mapped_column(String(64))
    canonical_location_id: Mapped[str] = mapped_column(String(64))
    region_id: Mapped[str] = mapped_column(String(64))
    mapping_version: Mapped[str] = mapped_column(String(32))
    content_hash: Mapped[str] = mapped_column(String(64))
    import_batch_id: Mapped[str] = mapped_column(ForeignKey("import_batches.id"))


# --------------------------------------------------------------------------
# Import registry
# --------------------------------------------------------------------------
class ImportBatch(Base):
    """One call to POST /imports/{dataset}. Re-importing the same file creates a
    NEW batch (so you can see it happened) but adds no new facts."""
    __tablename__ = "import_batches"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64))
    dataset: Mapped[str] = mapped_column(String(32))
    filename: Mapped[str | None] = mapped_column(String(255))
    file_sha256: Mapped[str] = mapped_column(String(64))
    # PROCESSING | COMPLETED | COMPLETED_WITH_ISSUES | FAILED
    status: Mapped[str] = mapped_column(String(24), default="PROCESSING")
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    accepted_rows: Mapped[int] = mapped_column(Integer, default=0)
    rejected_rows: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_rows: Mapped[int] = mapped_column(Integer, default=0)
    conflict_rows: Mapped[int] = mapped_column(Integer, default=0)
    updated_rows: Mapped[int] = mapped_column(Integer, default=0)  # roster updates / late re-mapping
    mapping_version: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    issues: Mapped[list["RowIssue"]] = relationship(back_populates="batch")


class RowIssue(Base):
    """Every row we did NOT cleanly accept. Nothing is silently dropped.

    kind: REJECTED (invalid), CONFLICT (same id, changed content),
          UNMAPPED (stored, but product/location mapping missing).
    Feeds GET /data-quality.
    """
    __tablename__ = "row_issues"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64))
    batch_id: Mapped[str] = mapped_column(ForeignKey("import_batches.id"))
    dataset: Mapped[str] = mapped_column(String(32))
    row_number: Mapped[int] = mapped_column(Integer)  # 1-based data row (header excluded)
    source_record_id: Mapped[str | None] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))
    reason_code: Mapped[str] = mapped_column(String(48))
    detail: Mapped[str] = mapped_column(Text)
    raw: Mapped[dict] = mapped_column(JSON)
    resolved: Mapped[bool] = mapped_column(default=False)

    batch: Mapped[ImportBatch] = relationship(back_populates="issues")
    __table_args__ = (Index("ix_row_issues_open", "tenant_id", "resolved", "kind"),)


# --------------------------------------------------------------------------
# Source facts
# --------------------------------------------------------------------------
class PoLine(Base):
    """ERP purchase-order line. Grain = one PO line (po_number + line_number)."""
    __tablename__ = "po_lines"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_system", "source_record_id", name="uq_po_source_identity"),
        Index("ix_po_business_key", "tenant_id", "po_number", "line_number"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64))
    source_system: Mapped[str] = mapped_column(String(16), default="ERP")
    source_record_id: Mapped[str] = mapped_column(String(128))
    po_number: Mapped[str] = mapped_column(String(32))
    line_number: Mapped[int] = mapped_column(Integer)
    supplier_code: Mapped[str] = mapped_column(String(32))
    # raw source values (kept for lineage)
    source_location_code: Mapped[str] = mapped_column(String(64))
    source_item_code: Mapped[str] = mapped_column(String(64))
    ordered_qty: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    ordered_uom: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24))  # OPEN | CANCELLED | ...
    expected_receipt_by: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # derived (NULL when mapping is missing/ambiguous)
    canonical_product_id: Mapped[str | None] = mapped_column(String(64))
    canonical_location_id: Mapped[str | None] = mapped_column(String(64))
    base_uom: Mapped[str | None] = mapped_column(String(16))
    ordered_qty_base: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    mapping_status: Mapped[str] = mapped_column(String(16))  # MAPPED | UNMAPPED
    mapping_version: Mapped[str] = mapped_column(String(32))
    content_hash: Mapped[str] = mapped_column(String(64))
    import_batch_id: Mapped[str] = mapped_column(ForeignKey("import_batches.id"))


class ReceiptLine(Base):
    """WMS goods-receipt line. Grain = one receipt line; MANY can link to one PO line."""
    __tablename__ = "receipt_lines"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_system", "source_record_id", name="uq_receipt_source_identity"),
        Index("ix_receipt_po_link", "tenant_id", "po_number", "po_line_number"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64))
    source_system: Mapped[str] = mapped_column(String(16), default="WMS")
    source_record_id: Mapped[str] = mapped_column(String(128))
    receipt_number: Mapped[str] = mapped_column(String(32))
    receipt_line: Mapped[int] = mapped_column(Integer)
    po_number: Mapped[str] = mapped_column(String(32))
    po_line_number: Mapped[int] = mapped_column(Integer)
    source_location_code: Mapped[str] = mapped_column(String(64))
    source_product_ref: Mapped[str] = mapped_column(String(64))
    accepted_qty: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    uom: Mapped[str] = mapped_column(String(16))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    posting_status: Mapped[str] = mapped_column(String(16))  # POSTED | ...
    canonical_product_id: Mapped[str | None] = mapped_column(String(64))
    canonical_location_id: Mapped[str | None] = mapped_column(String(64))
    base_uom: Mapped[str | None] = mapped_column(String(16))
    accepted_qty_base: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    mapping_status: Mapped[str] = mapped_column(String(16))
    mapping_version: Mapped[str] = mapped_column(String(32))
    content_hash: Mapped[str] = mapped_column(String(64))
    import_batch_id: Mapped[str] = mapped_column(ForeignKey("import_batches.id"))


class Employee(Base):
    """HRMS roster. location is OPTIONAL (regional roles are region-scoped)."""
    __tablename__ = "employees"
    __table_args__ = (UniqueConstraint("tenant_id", "employee_id", name="uq_employee"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64))
    employee_id: Mapped[str] = mapped_column(String(64))  # source_employee_id (HRMS)
    display_name: Mapped[str] = mapped_column(String(128))
    employment_status: Mapped[str] = mapped_column(String(16))  # ACTIVE | INACTIVE
    role: Mapped[str] = mapped_column(String(48))
    source_location_code: Mapped[str | None] = mapped_column(String(64))
    canonical_location_id: Mapped[str | None] = mapped_column(String(64))  # NULL for regional roles
    region_id: Mapped[str | None] = mapped_column(String(64))
    manager_employee_id: Mapped[str | None] = mapped_column(String(64))
    mapping_status: Mapped[str] = mapped_column(String(16))
    mapping_version: Mapped[str] = mapped_column(String(32))
    content_hash: Mapped[str] = mapped_column(String(64))
    import_batch_id: Mapped[str] = mapped_column(ForeignKey("import_batches.id"))


class WorkforceSnapshot(Base):
    """Sampled shift status. A snapshot is a point-in-time observation, so the
    identity is (employee, observed_at); newer observations are new rows."""
    __tablename__ = "workforce_snapshots"
    __table_args__ = (
        UniqueConstraint("tenant_id", "employee_id", "observed_at", name="uq_snapshot_identity"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64))
    employee_id: Mapped[str] = mapped_column(String(64))
    availability_status: Mapped[str] = mapped_column(String(16))  # ON_SHIFT | OFF_SHIFT
    shift_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    shift_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String(64))
    import_batch_id: Mapped[str] = mapped_column(ForeignKey("import_batches.id"))


# --------------------------------------------------------------------------
# Exceptions (business cases). A TASK is the work a person does; see tasks later.
# --------------------------------------------------------------------------
class ExceptionCase(Base):
    """One business case per stable case_key (e.g. RECEIPT_SHORTFALL:PO-501:1).

    UNIQUE(tenant_id, case_key) is what stops two detector runs / two workers from
    creating two cases for the same issue. Re-detection UPDATES this row (revision++).
    """
    __tablename__ = "exceptions"
    __table_args__ = (UniqueConstraint("tenant_id", "case_key", name="uq_exception_case_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64))
    case_key: Mapped[str] = mapped_column(String(128))
    exception_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24), default="OPEN")  # OPEN | AUTO_CLOSED | RESOLVED
    rule_version: Mapped[str] = mapped_column(String(48))
    mapping_version: Mapped[str] = mapped_column(String(32))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    po_number: Mapped[str] = mapped_column(String(32))
    po_line_number: Mapped[int] = mapped_column(Integer)
    supplier_code: Mapped[str] = mapped_column(String(32))
    canonical_product_id: Mapped[str] = mapped_column(String(64))
    canonical_location_id: Mapped[str] = mapped_column(String(64))
    expected_qty: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    received_qty: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    variance_qty: Mapped[Decimal] = mapped_column(Numeric(18, 6))   # expected - received
    unit: Mapped[str] = mapped_column(String(16))
    expected_receipt_by: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    summary: Mapped[str] = mapped_column(Text)
    known_facts: Mapped[list] = mapped_column(JSON)
    unknowns: Mapped[list] = mapped_column(JSON)
    evidence_needed: Mapped[list] = mapped_column(JSON)
    close_reason: Mapped[str | None] = mapped_column(Text)
    resolution_code: Mapped[str | None] = mapped_column(String(48))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    evidence: Mapped[list["ExceptionEvidence"]] = relationship(
        back_populates="exception", cascade="all, delete-orphan")


class ExceptionEvidence(Base):
    """Links a case to the exact source rows behind it (lineage)."""
    __tablename__ = "exception_evidence"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    exception_id: Mapped[str] = mapped_column(ForeignKey("exceptions.id"))
    role: Mapped[str] = mapped_column(String(24))   # PO_LINE | RECEIPT_COUNTED | RECEIPT_IGNORED
    record_type: Mapped[str] = mapped_column(String(24))
    record_id: Mapped[str] = mapped_column(String(36))   # internal row id
    source_system: Mapped[str] = mapped_column(String(16))
    source_record_id: Mapped[str] = mapped_column(String(128))
    import_batch_id: Mapped[str] = mapped_column(String(36))
    qty_base: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    note: Mapped[str | None] = mapped_column(Text)

    exception: Mapped[ExceptionCase] = relationship(back_populates="evidence")


# --------------------------------------------------------------------------
# Tasks (work a person performs) - Step 4
# --------------------------------------------------------------------------
TASK_STATUSES = ("CREATED", "ASSIGNED", "ACKNOWLEDGED", "IN_PROGRESS", "BLOCKED",
                 "COMPLETED", "PENDING_VERIFICATION", "VERIFIED")


class Task(Base):
    """Work for a person. One exception can have several tasks (task_type differs);
    reassigning a task never creates a second exception.

    DB-level safety:
      UNIQUE(tenant_id, task_key)  -> two workers cannot create two tasks for one issue
      CHECK status in (...)        -> arbitrary status strings can never be stored
      CHECK assignee required      -> only CREATED may be unassigned (manual-assignment queue)
    """
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_key", name="uq_task_key"),
        CheckConstraint("status IN ('CREATED','ASSIGNED','ACKNOWLEDGED','IN_PROGRESS','BLOCKED',"
                        "'COMPLETED','PENDING_VERIFICATION','VERIFIED')", name="ck_task_status"),
        CheckConstraint("status = 'CREATED' OR assignee_id IS NOT NULL", name="ck_task_assignee_required"),
        Index("ix_task_assignee", "tenant_id", "assignee_id", "status"),
        Index("ix_task_location", "tenant_id", "canonical_location_id", "status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64))
    task_key: Mapped[str] = mapped_column(String(200))      # "<case_key>:<task_type>"
    exception_id: Mapped[str] = mapped_column(ForeignKey("exceptions.id"))
    task_type: Mapped[str] = mapped_column(String(48))
    status: Mapped[str] = mapped_column(String(24), default="CREATED")
    canonical_location_id: Mapped[str] = mapped_column(String(64))
    assignee_id: Mapped[str | None] = mapped_column(String(64))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolution_code: Mapped[str | None] = mapped_column(String(48))
    evidence_ref: Mapped[str | None] = mapped_column(String(255))
    completed_by: Mapped[str | None] = mapped_column(String(64))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_by: Mapped[str | None] = mapped_column(String(64))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event_seq: Mapped[int] = mapped_column(Integer, default=0)   # last audit sequence number


class TaskAssignment(Base):
    """Assignment HISTORY. The active assignment is the row with ended_at IS NULL
    (a partial unique index allows at most one per task)."""
    __tablename__ = "task_assignments"
    __table_args__ = (
        Index("uq_task_active_assignment", "task_id", unique=True,
              postgresql_where=text("ended_at IS NULL")),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    assignee_id: Mapped[str | None] = mapped_column(String(64))   # NULL = needs manual assignment
    assigned_by: Mapped[str] = mapped_column(String(64))          # 'SYSTEM' or an employee id
    tier: Mapped[str] = mapped_column(String(16))                 # PRIMARY | FALLBACK | MANUAL | NONE
    reason: Mapped[str] = mapped_column(Text)                     # WHY this person / queue
    selection_trace: Mapped[list] = mapped_column(JSON)           # every candidate + verdict
    policy_version: Mapped[str] = mapped_column(String(32))
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[str | None] = mapped_column(Text)


class TaskEvent(Base):
    """Append-only audit trail: every state transition and assignment change."""
    __tablename__ = "task_events"
    __table_args__ = (UniqueConstraint("task_id", "seq", name="uq_task_event_seq"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    seq: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(32))
    from_status: Mapped[str | None] = mapped_column(String(24))
    to_status: Mapped[str | None] = mapped_column(String(24))
    actor_id: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class IdempotencyKey(Base):
    """Client retry protection: same key + same request => the ORIGINAL response is replayed."""
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_idempotency_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64))
    key: Mapped[str] = mapped_column(String(128))
    fingerprint: Mapped[str] = mapped_column(String(64))   # hash of (task, action, actor, body)
    response: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
