# Architecture note

## 1. Flow

```
CSV (ERP / WMS / HRMS / WFM / crosswalks)
   │  POST /imports/{dataset}
   ▼
[ingestion]  validate (typed, tz-aware) ─► map ids + convert UOM ─► INSERT … ON CONFLICT DO NOTHING
   │            └ bad row → row_issues (REJECTED)      └ same id, new content → row_issues (CONFLICT)
   │                                                   └ missing alias → stored UNMAPPED + row_issues (UNMAPPED)
   ▼
source facts: po_lines · receipt_lines · employees · workforce_snapshots   (+ import_batches, row_issues)
   │  POST /detection/run          (pure rule in detection/rules.py, DB layer in detection/service.py)
   ▼
exceptions + exception_evidence    case_key UNIQUE → one case per PO line, updated not duplicated
   │  POST /tasks/generate         (pure policy in routing/policy.py)
   ▼
tasks · task_assignments · task_events      server-enforced state machine, append-only audit
   │  verify by a different authorised person
   ▼
exception RESOLVED
```

Layers are kept apart on purpose: **parsing/mapping** (source specific) → **canonical facts** → **rule** (pure
function, no DB/clock) → **persistence** → **routing policy** (pure) → **workflow**. A second retailer needs a new
entry in `app/ingestion/datasets.py` (schema + build function); detector, routing and workflow do not change.

## 2. Data model (tables)

| Table | Grain / identity | Notes |
|---|---|---|
| `product_aliases`, `location_aliases` | `(tenant, source_system, external id)` unique | crosswalk to canonical ids; product alias also holds source UOM → base UOM factor |
| `import_batches` | one per upload | status, counts (accepted / rejected / duplicate / conflict / updated), file sha256, mapping version |
| `row_issues` | row of a batch | `REJECTED`, `CONFLICT`, `UNMAPPED`; raw row kept; feeds `/data-quality` |
| `po_lines` | `(tenant, 'ERP', source_record_id)` unique; business key `(po_number, line_number)` | raw source values + derived canonical ids + `ordered_qty_base`, `content_hash`, `import_batch_id`, `mapping_version` |
| `receipt_lines` | `(tenant, 'WMS', source_record_id)` unique; links to a PO line by `(po_number, po_line_number)` | many receipts per PO line |
| `employees` | `(tenant, employee_id)` unique | `canonical_location_id` is nullable (regional roles are region-scoped) |
| `workforce_snapshots` | `(tenant, employee_id, observed_at)` unique | point-in-time observations; latest at/before `now` is used |
| `exceptions` | `(tenant, case_key)` unique | quantities, rule + mapping version, neutral explanation, evidence needed, revision |
| `exception_evidence` | per linked source row | role `PO_LINE` / `RECEIPT_COUNTED` / `RECEIPT_IGNORED` (+ why), source system/id, import batch |
| `tasks` | `(tenant, task_key)` unique | CHECK on status values; CHECK that only `CREATED` may be unassigned |
| `task_assignments` | history; partial unique index = one active row per task | who, why, tier, full candidate `selection_trace`, policy version |
| `task_events` | `(task_id, seq)` unique | append-only audit: actor, reason, from/to status, payload, idempotency key |
| `idempotency_keys` | `(tenant, key)` unique | fingerprint of the request + stored response |

## 3. Which system is authoritative for what

| Field | Authority | Where it lives here |
|---|---|---|
| Ordered qty, UOM, PO status, expected-receipt deadline, supplier | ERP | `po_lines` (raw, never overwritten) |
| Accepted qty, posting status, received time, GRN split | WMS | `receipt_lines` (raw) |
| Product and location **identity**, UOM conversion | the crosswalk files (OPSiB canonical mapping) | `*_aliases`; derived columns on facts |
| Employment status, role, location, manager | HRMS | `employees` (employment status always wins) |
| Shift / availability | workforce management | `workforce_snapshots` (advisory; only used if fresh) |
| Exceptions, tasks, assignments, audit, resolution | OPSiB | its own tables; never written back to a source system |

Raw input and business state are separate: source columns are stored exactly as received; canonical ids and base
quantities are **derived** columns that may be NULL.

## 4. Missing, ambiguous, conflicting data

| Situation | Behaviour |
|---|---|
| Invalid row (negative qty, bad enum, timestamp without offset, missing field) | `REJECTED` with reason, visible in the import response and `/data-quality`; other rows still load; HTTP `207` |
| Missing file columns | whole file refused (`422`), batch recorded as `FAILED` |
| No alias for product/location, or row UOM ≠ alias UOM | row stored `UNMAPPED` + `UNMAPPED` issue; **not** evaluated by the detector (a data-quality item, not a business exception). When the alias arrives later, re-importing the same file back-fills derived columns and closes the issue |
| Identical repeat of a source id | counted as `duplicate`, no new fact |
| Same source id, changed content | `CONFLICT` issue, original row kept; any PO line that depends on it is reported `DATA_QUALITY_CONFLICT` and its existing case is left untouched |
| Two source ids for one PO line | same: conflict, not evaluated |
| Receipt for another product/location than its PO | `DATA_QUALITY_MISMATCH`, not evaluated |
| Late receipt | next detection run: case updated or `AUTO_CLOSED` |
| Import fails half-way (unexpected error) | one transaction per file → nothing from that file is kept, batch marked `FAILED`; re-running is safe |
| Stale / missing shift snapshot | person is not eligible; reason recorded in `selection_trace` |

## 5. Idempotency and concurrency (where the guarantee actually comes from)

1. **Import**: unique index on source identity + `ON CONFLICT DO NOTHING`. A concurrent second writer blocks on the
   index until the first commits, then inserts nothing; we then compare `content_hash` to classify duplicate vs
   conflict. Re-importing everything is a no-op (`test_importing_everything_twice_changes_nothing`).
2. **Exceptions**: unique `case_key` (business grain, not run id) + `ON CONFLICT DO NOTHING`; existing row is locked
   `FOR UPDATE` and updated, `revision + 1` only if quantities/status changed.
3. **Tasks**: unique `task_key`; only the inserting worker writes `TASK_CREATED` + first assignment
   (`test_two_concurrent_workers_create_one_task_per_exception`, 4 threads).
4. **Task actions**: `SELECT … FOR UPDATE` on the task row serialises concurrent actions; `seq` for audit events is
   allocated under that lock. The idempotency key row is inserted in the same transaction as the change.
5. **Rollback hygiene**: any error rolls back the change *and* the idempotency key, so a failed request can be
   retried with the same key.

## 6. Detection rule (`receipt-shortfall-v1`)

Pure function `evaluate_line(po, receipts, now, tolerance, conflicted_ids)` in `app/detection/rules.py`:
cancelled/non-open → excluded; unmapped → data quality; `now <= deadline` → not yet actionable; count `POSTED`
receipts with `received_at <= now`; mismatch/unmapped/conflict → data quality; else
`variance = expected_base − Σ accepted_base`, shortfall iff `variance > tolerance`. Grain is preserved: the unit of
evaluation is the PO line, receipts are summed, never counted as rows.

Required scenarios (all asserted in `test_required_scenarios_on_fixture_pack`):
PO-501 → 8 EA · PO-502 → none · PO-503 → not yet actionable · PO-504 → excluded (cancelled) ·
PO-505 → 120 EA vs 96 EA = 24 EA · PO-506 → 10 EA (duplicate receipt row counted once).

**Why "receipt shortfall" and not "supplier fault":** the data has PO quantities and WMS receipts only. Nothing shows
what the supplier dispatched, what the carrier delivered, or whether receipts are pending. Each finding therefore
states what is known, what is not, and which evidence would establish cause (dispatch/ASN, carrier POD,
unposted warehouse receipts, short-close confirmation).

## 7. Routing and workflow

* Policy `routing-v1` (data in `POLICIES`): primary `receiving_associate` at the same canonical location, fallback
  `store_manager`; otherwise the task stays visible in the manual queue. Eligible = `ACTIVE` ∧ canonical location ∧
  fresh snapshot (≤ 60 min) ∧ `ON_SHIFT` ∧ `shift_start ≤ now ≤ shift_end`.
  Employment status always overrides shift data (HR-103). Region-scoped roles (HR-501) are not auto-assigned to a
  store task.
* Manual reassignment is held to the **same** eligibility rule, so an ineligible person can never be assigned by
  any path.
* State machine in `app/workflows/state_machine.py`; the client sends actions, never statuses. Actions are gated
  by who may perform them (assignee for work; store manager of that location or regional lead of its region for
  reassign/verify/reject; verifier ≠ worker).

## 8. AI boundary

Deterministic detector → typed finding + evidence ids → (optional) small evidence package → provider → output
validated against a strict schema (no quantity/status/assignee fields), every cited ref checked against the package,
no new numbers, no assertive cause claims → otherwise **fallback to the deterministic text**. Default provider is
`disabled`. Employee data is never sent. See `app/ai/summary.py`.

## 9. Adding another retailer export

Add a row schema in `app/ingestion/schemas.py`, a `build_*` function and a `DatasetSpec` in
`app/ingestion/datasets.py` that outputs the same canonical fields (`canonical_product_id`, `canonical_location_id`,
base uom, base quantities, status, deadline). `detection/` and `routing/` read only canonical columns.

## 10. What must change before multi-tenant production data

Tenant from the auth token on every request and every query (plus RLS as a backstop); per-tenant configuration for
mapping/rule/routing versions; secrets in a vault; migrations only through Alembic in CI/CD; metrics, alerts and
dead-letter handling for imports; retention and deletion policy for employee data and raw rows; background workers
with leader election or queue; streamed/size-limited uploads; real clock in `app/clock.py`.