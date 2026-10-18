"""AI boundary: validation, fallback, and the 'AI is optional' guarantee. No network, no cost."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.ai.evidence import package_from_case
from app.ai.providers import ClaudeProvider, DisabledProvider, ScriptedProvider
from app.ai.summary import SummaryRejected, summarize, validate_summary
from app.api.investigation import provider_dep
from app.db.models import ExceptionCase
from app.detection.service import run_detection
from app.main import app

GOOD = {
    "explanation": "PO-501 line 1 has posted receipts below the ordered quantity after the deadline.",
    "missing_context": ["No dispatch or ASN data is available."],
    "suggested_steps": [{"text": "Check whether further receipts for PO-501 are still expected.",
                         "cites": ["RECEIPT_COUNTED:WMS-GRN501-A-L1"]}],
    "cited_refs": ["PO_LINE:ERP-PO501-L1", "RECEIPT_COUNTED:WMS-GRN501-B-L1"],
}


@pytest.fixture()
def case(session, load_all):
    load_all()
    run_detection(session)
    return session.scalars(select(ExceptionCase).where(ExceptionCase.po_number == "PO-501")).one()


@pytest.fixture()
def pkg(case):
    return package_from_case(case)


# ------------------------------------------------------------------ what we SEND
def test_package_has_only_case_facts_and_evidence_refs(pkg):
    dump = pkg.model_dump_json()
    assert pkg.facts["variance"] == "8 EA" and pkg.facts["expected"] == "100 EA"
    assert {"PO_LINE:ERP-PO501-L1", "RECEIPT_COUNTED:WMS-GRN501-A-L1",
            "RECEIPT_COUNTED:WMS-GRN501-B-L1"} == pkg.refs()
    for private in ("HR-102", "Dev Shah", "assignee", "employee"):       # no people data leaves the system
        assert private not in dump


# ------------------------------------------------------------------ what we ACCEPT
def test_valid_output_is_accepted(pkg):
    s = validate_summary(json.dumps(GOOD), pkg)
    assert s.cited_refs and s.suggested_steps[0].cites == ["RECEIPT_COUNTED:WMS-GRN501-A-L1"]


@pytest.mark.parametrize("extra", [{"status": "VERIFIED"}, {"variance_qty": "9 EA"},
                                   {"assignee_id": "HR-101"}, {"verified": True}])
def test_model_cannot_smuggle_authoritative_fields(pkg, extra):
    with pytest.raises(SummaryRejected, match="schema violation"):
        validate_summary({**GOOD, **extra}, pkg)


def test_not_json_is_rejected(pkg):
    with pytest.raises(SummaryRejected, match="not valid JSON"):
        validate_summary("Sure! Here is my analysis...", pkg)


def test_citing_a_record_that_was_not_supplied_is_rejected(pkg):
    bad = {**GOOD, "cited_refs": ["RECEIPT_COUNTED:WMS-GRN999-Z-L1"]}
    with pytest.raises(SummaryRejected, match="not in the evidence"):
        validate_summary(bad, pkg)
    bad = {**GOOD, "suggested_steps": [{"text": "x", "cites": ["PO_LINE:INVENTED"]}]}
    with pytest.raises(SummaryRejected, match="not in the evidence"):
        validate_summary(bad, pkg)


def test_invented_quantity_is_rejected(pkg):
    bad = {**GOOD, "explanation": "PO-501 is short by 777 units."}       # 777 can never be in the evidence
    with pytest.raises(SummaryRejected, match="numbers not present"):
        validate_summary(bad, pkg)


def test_quantity_that_is_in_the_evidence_is_allowed(pkg):
    ok = {**GOOD, "explanation": "Posted receipts total 92 EA against 100 EA ordered."}
    assert validate_summary(ok, pkg).explanation.startswith("Posted receipts")


def test_assertive_cause_is_rejected(pkg):
    bad = {**GOOD, "explanation": "The supplier is at fault for PO-501."}
    with pytest.raises(SummaryRejected, match="cause"):
        validate_summary(bad, pkg)


# ------------------------------------------------------------------ the app survives the model
def test_ok_path(pkg):
    r = summarize(pkg, ScriptedProvider(GOOD), 1.0)
    assert r.status == "AI_OK" and r.source == "scripted" and r.summary and r.reason is None


@pytest.mark.parametrize("provider,fragment", [
    (ScriptedProvider("not json"), "output rejected"),
    (ScriptedProvider({**GOOD, "status": "VERIFIED"}), "schema violation"),
    (ScriptedProvider(GOOD, error=RuntimeError("boom")), "provider error: RuntimeError"),
    (ScriptedProvider(GOOD, delay=0.5), "timed out"),
    (DisabledProvider(), "disabled"),
])
def test_every_failure_mode_falls_back_without_raising(pkg, provider, fragment):
    r = summarize(pkg, provider, 0.1)
    assert r.status == "FALLBACK" and r.summary is None and fragment in r.reason


def test_claude_provider_with_fake_client(pkg):
    calls = []

    class FakeMessages:
        def create(self, **kw):
            calls.append(kw)
            return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(GOOD))])

    provider = ClaudeProvider(SimpleNamespace(messages=FakeMessages()), "test-model")
    r = summarize(pkg, provider, 1.0)
    assert r.status == "AI_OK"
    sent = json.loads(calls[0]["messages"][0]["content"])
    assert sent["facts"]["variance"] == "8 EA"            # model is GIVEN the number, never asked to compute it
    assert "never claim supplier fault" in calls[0]["system"]


# ------------------------------------------------------------------ API
def test_endpoint_works_with_ai_disabled(client, case):
    r = client.get(f"/exceptions/{case.id}/investigation-summary")
    body = r.json()
    assert r.status_code == 200
    assert body["ai"]["status"] == "FALLBACK" and "disabled" in body["ai"]["reason"]
    assert "not proof of supplier fault" in " ".join(body["deterministic"]["unknowns"])


def test_endpoint_returns_validated_ai_output(client, case):
    app.dependency_overrides[provider_dep] = lambda: ScriptedProvider(GOOD)
    try:
        body = client.get(f"/exceptions/{case.id}/investigation-summary").json()
    finally:
        app.dependency_overrides.pop(provider_dep, None)
    assert body["ai"]["status"] == "AI_OK" and body["ai"]["summary"]["cited_refs"]
    assert body["deterministic"]["summary"].startswith("Receipt shortfall of 8 EA")   # authority stays deterministic


def test_endpoint_falls_back_when_model_returns_garbage(client, case):
    app.dependency_overrides[provider_dep] = lambda: ScriptedProvider({"explanation": "x", "status": "VERIFIED"})
    try:
        body = client.get(f"/exceptions/{case.id}/investigation-summary").json()
    finally:
        app.dependency_overrides.pop(provider_dep, None)
    assert body["ai"]["status"] == "FALLBACK" and body["ai"]["summary"] is None
    assert body["deterministic"]["known"]


def test_endpoint_404(client):
    assert client.get("/exceptions/nope/investigation-summary").status_code == 404