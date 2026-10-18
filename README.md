# OPSiB Backend Assessment: receipt-shortfall slice

A small FastAPI + PostgreSQL service that imports retail ERP / WMS / HRMS exports, reconciles them into a
canonical model, detects **receipt shortfalls** against purchase orders, routes a task to an eligible person,
and records the task lifecycle with an audit trail.

> A shortfall here means *posted receipts are below the ordered quantity after the deadline*.
> It is **not** proof of supplier fault or financial loss: the fixtures contain no dispatch/ASN data.

Docs: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) · [`docs/API_EXAMPLES.md`](docs/API_EXAMPLES.md) ·
[`docs/AI_DISCLOSURE.md`](docs/AI_DISCLOSURE.md). OpenAPI is served at `/docs` and `/openapi.json`.

## Quick start

Requirements: Python 3.10+, PostgreSQL 14+.

```bash
python -m venv .venv && source .venv/bin/activate      # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

# PostgreSQL: either Docker ...
docker compose up -d                                   # creates databases opsib and opsib_test
# ... or your own server: create user/db  opsib / opsib / opsib  and a database  opsib_test

cp .env.example .env                                   # optional: defaults already match the Docker setup

alembic upgrade head                                   # create the schema (production path)
python -m app.ingestion.cli data/fixtures              # import the six CSVs (dependency order)
python -m app.detection.cli                            # detect shortfalls at the frozen time
python -m app.routing.cli                              # create + route tasks
uvicorn app.main:app --reload                          # http://localhost:8000/docs
```

`python -m app.db.init --reset` is a dev shortcut (drop + create_all) and is not used in production.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://opsib:opsib@localhost:5432/opsib` | App database |
| `TEST_DATABASE_URL` | `.../opsib_test` | Used by the tests (schema is dropped and recreated per test) |
| `TENANT_ID` | `tenant-demo` | Single configured tenant (see "Not implemented") |
| `EVALUATION_TIME` | `2026-10-16T15:00:00+05:30` | **Frozen scenario time.** Must carry a UTC offset. The machine clock is never used for scenario logic |
| `MAPPING_VERSION` / `RULE_VERSION` | `map-v1` / `receipt-shortfall-v1` | Stamped on every record / finding |
| `SHORTFALL_TOLERANCE_BASE` | `0` | Absolute tolerance in base units (zero tolerance by default) |
| `TASK_DUE_HOURS` | `24` | Task due = creation time + this |
| `AVAILABILITY_MAX_AGE_MINUTES` | `60` | A shift snapshot older than this is "stale" and cannot be used for routing |
| `AI_PROVIDER` | `none` | `none` (default, fully deterministic) or `claude` |
| `AI_TIMEOUT_SECONDS`, `AI_MODEL` | `5`, `claude-sonnet-5-5` | Only used when `AI_PROVIDER=claude` |
| `ANTHROPIC_API_KEY` | unset | Read from the environment only if `AI_PROVIDER=claude` |

## Tests

```bash
python -m pytest -q          # needs PostgreSQL and the opsib_test database
```

Result when this was prepared: **114 passed**. Breakdown:

| File | What it covers |
|---|---|
| `tests/test_ingestion.py` | validation, mapping, UOM, idempotent re-import, conflicting ids, unmapped rows, late aliases, API codes |
| `tests/test_detection_rules.py` | pure rule: UOM, partial receipts, deadline boundary, cancelled, tolerance, data-quality paths |
| `tests/test_detection_integration.py` | **the six required PO scenarios on the real fixtures**, lineage, double import/detect, late receipt |
| `tests/test_routing.py` | eligibility (off-shift, inactive-but-ON_SHIFT, stale, fallback manager, manual queue), concurrent task creation |
| `tests/test_workflow.py` | state machine, invalid transitions, authorisation, audit trail, retries, idempotency keys, reassignment, DB guards |
| `tests/test_ai.py` | AI output validation, fallback on every failure mode, app works with AI disabled |

## Endpoints

| Method + path | Purpose |
|---|---|
| `POST /imports/{dataset}` | Upload one CSV. `201` clean, `207` loaded with row-level issues, `404` unknown dataset, `422` bad file |
| `GET /imports/{id}` | Counts, status and row-level errors |
| `GET /data-quality` | Open rejected / conflicting / unmapped rows |
| `POST /detection/run` | Re-evaluate every PO line at the frozen time (safe to repeat) |
| `GET /exceptions`, `GET /exceptions/{id}` | Findings with evidence, rule version, known/unknown, linked tasks |
| `GET /exceptions/{id}/investigation-summary` | Deterministic explanation + optional validated AI summary |
| `POST /tasks/generate` | Create/route one task per open exception (idempotent; retries the manual queue) |
| `GET /tasks?assignee_id=&location=&status=&needs_manual_assignment=` | Task lists with status and due time |
| `GET /tasks/{id}` | Task with full audit events and assignment history |
| `POST /tasks/{id}/acknowledge · start · block · unblock · complete · verify · reject · reassign` | Validated transitions. Header `X-Actor-Id` (required), `Idempotency-Key` (optional) |

Errors always look like `{"error": "<code>", "detail": "<message>"}`.

## The three design questions, in one paragraph each

* **Second import / concurrent worker** – every source fact is written with
  `INSERT ... ON CONFLICT (tenant_id, source_system, source_record_id) DO NOTHING`, so the unique index decides
  the winner, not application code. An identical repeat is counted as a duplicate; changed content under the
  same id becomes a `CONFLICT` issue and the original row is kept.
* **Two workers, two tasks** – exceptions are keyed by `case_key` (`RECEIPT_SHORTFALL:PO-501:1`) and tasks by
  `task_key`, both `UNIQUE (tenant_id, key)` with `ON CONFLICT DO NOTHING`; only the worker whose insert actually
  inserted writes the creation event and first assignment.
* **Commit succeeded, client timed out** – a retry with the same `Idempotency-Key` replays the stored response;
  without a key, the same actor repeating the same completion is a no-op, a different payload gets `409`. The key
  row is written in the same transaction as the state change.

## Assumptions and trade-offs

* A PO line is `(po_number, line_number)`. Two different source ids for the same PO line are flagged as a conflict
  and not evaluated.
* The deadline has passed only when `now` is **strictly after** `expected_receipt_by`. Receipts after the deadline
  but at/before `now` still count. Receipts dated after `now` and non-`POSTED` receipts are ignored (and listed as
  ignored evidence).
* A receipt for a different product/location than its PO line, an unmapped receipt, or a receipt in an open
  conflict stops evaluation of that line: it is reported as a data-quality outcome, never as a business exception.
* If a late receipt removes the shortfall, the open case becomes `AUTO_CLOSED`; if quantities change, the same
  case is updated (`revision + 1`). A human-`RESOLVED` case is never silently changed.
* HRMS roster rows **update in place** when content changes (a termination must take effect), whereas every other
  dataset uses the conflict policy. The roster keeps only the latest state, not history.
* Routing picks the lowest `employee_id` among eligible people (deterministic; no load balancing).
* `COMPLETED` is transient: completing a task immediately moves it to `PENDING_VERIFICATION`. An exception is
  resolved only when **all** its tasks are verified by an authorised person other than the one who did the work.
* Timestamps in audit rows use the frozen clock (`app/clock.py`) so results are reproducible. In production this
  one function would return wall-clock time.
* Quantities are `NUMERIC(18,6)` and appear in JSON as decimal strings (for example `"8.000000"`).

## Not implemented (documented only): what a live multi-tenant pilot needs

* **Authentication / tenant authorisation.** `X-Actor-Id` is a stand-in for an authenticated user. Production must
  take identity and tenant from a verified token, enforce tenant on every query, and ideally use Postgres
  row-level security. Today the tenant is a single configured value.
* **Secrets management** (vault / cloud secret store, rotation) for DB and integration credentials.
* **Monitoring and alerting** (metrics, tracing, error tracking, dead-letter handling for failed imports).
  Structured logs exist and contain ids only (no names or other personal data), but nothing is shipped anywhere.
* **Retention and privacy policy** for employee data and raw import rows; archival of old batches.
* **Integration credentials and scheduling** for pulling from ERP/WMS/HRMS instead of uploaded CSVs; background
  worker or scheduler (here, detection and routing run when called).
* **Upload limits and streaming** for large files (files are currently read into memory).
* **Employee history / SCD**, load-balanced assignment, SLA escalation, notifications.
* The live Claude API call path has not been run against the real API (no key or spend); it is tested only with a
  fake client.

## Layout

```
app/api/        HTTP routers and response schemas
app/ingestion/  CSV schemas, dataset registry (add a retailer here), generic idempotent loader
app/mapping/    source id -> canonical id, UOM conversion
app/detection/  pure shortfall rule (rules.py) + DB layer (service.py)
app/routing/    pure eligibility policy (policy.py) + DB layer (service.py)
app/workflows/  state machine (pure), actions + audit + idempotency (service.py)
app/ai/         evidence package, provider interface, output validation and fallback
app/db/         SQLAlchemy models and session
migrations/     Alembic (0001 = full schema)
tests/          pytest
data/fixtures/  the supplied CSVs, unmodified
```