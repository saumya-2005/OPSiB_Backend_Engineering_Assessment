# API examples

Responses below were captured from a real run on the supplied fixtures (frozen time `2026-10-16T15:00:00+05:30`);
ids and some long fields are shortened. Interactive docs: `GET /docs`; export: `GET /openapi.json`.

Load order: `location_aliases`, `product_aliases`, `employees`, `workforce_status`, `purchase_order_lines`,
`goods_receipt_lines` (the CLI does this: `python -m app.ingestion.cli data/fixtures`).

## 1. Import a dataset

```bash
curl -X POST localhost:8000/imports/goods_receipt_lines -F "file=@data/fixtures/goods_receipt_lines.csv"
```
`201 Created`
```json
{ "id": "b48636b2-…", "dataset": "goods_receipt_lines", "status": "COMPLETED",
  "total_rows": 7, "accepted_rows": 6, "rejected_rows": 0, "duplicate_rows": 1, "conflict_rows": 0,
  "updated_rows": 0, "issues": [] }
```
The identical duplicate row is counted once as `duplicate_rows`. Importing the same file again gives
`accepted_rows: 0, duplicate_rows: 7` and changes nothing.

### Partial failure is not reported as success
Same endpoint with a bad row → `207 Multi-Status`
```json
{ "status": "COMPLETED_WITH_ISSUES", "rejected_rows": 1,
  "issues": [ { "row_number": 1, "source_record_id": "ERP-X1", "kind": "REJECTED",
    "reason_code": "VALIDATION_ERROR",
    "detail": "ordered_qty: Input should be greater than 0; expected_receipt_by: Input should have timezone info",
    "raw": { "po_number": "PO-900", "ordered_qty": "-5", "expected_receipt_by": "2026-10-15T12:00:00" },
    "resolved": false } ] }
```
Other statuses: `404` unknown dataset, `422` missing required columns.

`GET /imports/{id}` returns the same body. `GET /data-quality?kind=UNMAPPED` lists open issues:
`{ "open_issue_count": 1, "by_kind": {"REJECTED": 1}, "issues": [ … ] }`.

## 2. Detect shortfalls

```bash
curl -X POST localhost:8000/detection/run
```
`200` (abridged: one entry per PO line)
```json
{ "evaluated_at": "2026-10-16T15:00:00+05:30", "rule_version": "receipt-shortfall-v1",
  "lines": [
   {"po_number":"PO-501","outcome":"SHORTFALL","expected":"100.000000","received":"92.000000","variance":"8.000000","case_action":"CREATED"},
   {"po_number":"PO-502","outcome":"NO_SHORTFALL","expected":"40.000000","received":"40.000000","variance":"0.000000","case_action":null},
   {"po_number":"PO-503","outcome":"NOT_YET_ACTIONABLE","expected":"50.000000","received":null,"variance":null,"case_action":null},
   {"po_number":"PO-504","outcome":"EXCLUDED_CANCELLED","expected":null,"received":null,"variance":null,"case_action":null},
   {"po_number":"PO-505","outcome":"SHORTFALL","expected":"120.000000","received":"96.000000","variance":"24.000000","case_action":"CREATED"},
   {"po_number":"PO-506","outcome":"SHORTFALL","expected":"20.000000","received":"10.000000","variance":"10.000000","case_action":"CREATED"} ] }
```
Calling it again returns `case_action: "UNCHANGED"` for those three and creates nothing.
Other outcomes you can see with messier data: `DATA_QUALITY_UNMAPPED`, `DATA_QUALITY_CONFLICT`,
`DATA_QUALITY_MISMATCH`, `EXCLUDED_STATUS`.

## 3. Inspect an exception

`GET /exceptions` (filters: `status`, `location`) and `GET /exceptions/{id}`:
```json
{ "case_key": "RECEIPT_SHORTFALL:PO-501:1", "status": "OPEN", "canonical_location_id": "STORE-014",
  "expected_qty": "100.000000", "received_qty": "92.000000", "variance_qty": "8.000000", "unit": "EA",
  "rule_version": "receipt-shortfall-v1", "mapping_version": "map-v1", "revision": 1,
  "summary": "Receipt shortfall of 8 EA: PO PO-501 L1 expected 100 EA, posted receipts 92 EA. Cause not established.",
  "unknowns": [
    "Whether the supplier dispatched the full ordered quantity: no dispatch/ASN data is available.",
    "Whether any quantity was lost, damaged or delayed in transit, and where the difference arose.",
    "Whether further receipts are still expected or the line has been short-closed with the supplier.",
    "No cause is established. This is a receipt shortfall against the PO, not proof of supplier fault or financial loss." ],
  "evidence_needed": [ "Supplier dispatch / ASN quantity for PO PO-501 line 1",
    "Carrier delivery / proof-of-delivery records",
    "Warehouse receiving records, including unposted or pending goods receipts",
    "Supplier or buyer confirmation of any short-close or planned follow-up delivery" ],
  "evidence": [
    {"role":"PO_LINE","source_system":"ERP","source_record_id":"ERP-PO501-L1","qty_base":"100.000000","import_batch_id":"a86bfb14-…"},
    {"role":"RECEIPT_COUNTED","source_system":"WMS","source_record_id":"WMS-GRN501-A-L1","qty_base":"60.000000","import_batch_id":"b48636b2-…"},
    {"role":"RECEIPT_COUNTED","source_system":"WMS","source_record_id":"WMS-GRN501-B-L1","qty_base":"32.000000","import_batch_id":"b48636b2-…"} ],
  "tasks": [ {"id":"7542df84-…","status":"ASSIGNED","assignee_id":"HR-102","due_at":"2026-10-17T15:00:00+05:30"} ] }
```

## 4. Create and route tasks

```bash
curl -X POST localhost:8000/tasks/generate
```
```json
[ {"case_key":"RECEIPT_SHORTFALL:PO-501:1","action":"CREATED","status":"ASSIGNED","assignee_id":"HR-102"},
  {"case_key":"RECEIPT_SHORTFALL:PO-505:1","action":"CREATED","status":"ASSIGNED","assignee_id":"HR-401"},
  {"case_key":"RECEIPT_SHORTFALL:PO-506:1","action":"CREATED","status":"ASSIGNED","assignee_id":"HR-102"} ]
```
HR-101 (off shift) and HR-103 (inactive, although the shift export says ON_SHIFT) are never selected; STORE-015 has no
receiving associate, so the fallback store manager HR-401 gets it. If nobody is eligible: `"status":"CREATED",
"assignee_id":null` and the task appears in `GET /tasks?needs_manual_assignment=true`; running generate again after the
roster is fixed returns `"action":"ASSIGNED_LATE"`.

`GET /tasks?assignee_id=HR-102` → list with `status` and `due_at`.
`GET /tasks/{id}` → task + `events` + `assignments` (each with `reason` and the full `selection_trace`).

## 5. Work a task

Every action needs `X-Actor-Id`; `Idempotency-Key` is optional but recommended.

```bash
H='-H "X-Actor-Id: HR-102"'
curl -X POST localhost:8000/tasks/$ID/acknowledge -H "X-Actor-Id: HR-102"
curl -X POST localhost:8000/tasks/$ID/start       -H "X-Actor-Id: HR-102"
curl -X POST localhost:8000/tasks/$ID/complete    -H "X-Actor-Id: HR-102" -H "Idempotency-Key: req-123" \
     -H "Content-Type: application/json" \
     -d '{"resolution_code":"QUANTITY_CONFIRMED_SHORT","evidence_ref":"GRN-AUDIT-7781"}'
```
`200` → `{ "replayed": false, "task": { "status": "PENDING_VERIFICATION", "resolution_code": "...", ... } }`

**Client timed out and retries the exact same request** → `200`, header `Idempotent-Replay: true`,
body `{ "replayed": true, "task": { …same as before… } }`; no second event is written.
Same key with a different body → `422 idempotency_key_reuse`. Same completion without a key by the same person →
also a no-op; a different payload → `409 conflict`.

Verification by a different authorised person (store manager of STORE-014, or the regional lead):
```bash
curl -X POST localhost:8000/tasks/$ID/verify -H "X-Actor-Id: HR-201"
```
→ task `VERIFIED`, exception `RESOLVED`.

Resolution codes: `QUANTITY_CONFIRMED_SHORT`, `RECEIPT_POSTED_LATE`, `DATA_CORRECTION_REQUESTED`,
`SUPPLIER_CLAIM_RAISED`, `NO_ACTION_REQUIRED`.

### Reassign
```bash
curl -X POST localhost:8000/tasks/$ID/reassign -H "X-Actor-Id: HR-201" -H "Content-Type: application/json" \
     -d '{"assignee_id":"HR-201","reason":"receiver is on a delivery"}'
```
Allowed only for managers of that location (or the regional lead); the new assignee must pass the same
eligibility check (so `HR-101`, `HR-103`, other-store staff, unknown ids → `422`).

### Errors (uniform shape)
```json
{"error":"invalid_transition","detail":"action VERIFY is not allowed while task is IN_PROGRESS"}   // 409
{"error":"forbidden","detail":"only the current assignee can perform this action"}                  // 403
{"error":"validation_failed","detail":"'evidence_ref' is required"}                                // 422
{"error":"not_found","detail":"task not found"}                                                    // 404
```

### Audit trail of the happy path (`GET /tasks/{id}` → `events`)
```
 1 TASK_CREATED           SYSTEM  None -> CREATED
 2 ASSIGNED               SYSTEM  CREATED -> ASSIGNED
 3 ACKNOWLEDGED           HR-102  ASSIGNED -> ACKNOWLEDGED
 4 STARTED                HR-102  ACKNOWLEDGED -> IN_PROGRESS
 5 COMPLETED              HR-102  IN_PROGRESS -> COMPLETED
 6 SENT_FOR_VERIFICATION  SYSTEM  COMPLETED -> PENDING_VERIFICATION
 7 VERIFIED               HR-201  PENDING_VERIFICATION -> VERIFIED
 8 EXCEPTION_RESOLVED     SYSTEM  VERIFIED -> VERIFIED
```

## 6. Investigation summary (optional AI, validated)

`GET /exceptions/{id}/investigation-summary` always returns the deterministic explanation; the `ai` part is present
only if a provider is configured and its output passed validation:
```json
{ "deterministic": { "summary": "...", "known": [...], "unknowns": [...], "evidence_needed": [...] },
  "ai": { "status": "FALLBACK", "source": "disabled", "reason": "AI provider disabled (AI_PROVIDER=none)", "summary": null } }
```