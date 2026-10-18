# AI tooling disclosure

The assessment allows AI coding tools provided they are disclosed and the candidate is responsible for every line.

## Tools used

* **Claude** (Anthropic), used as a coding assistant through the Claude app / Claude Code: it drafted most of the
  source code, the tests, the Alembic setup and these documents from the assessment PDF and fixtures, and ran the
  test suite in its own environment. Local test runs of the submitted code on my machine are listed below.
* No AI tool is called at run time. `AI_PROVIDER=none` is the default; the optional Claude provider is only wired
  behind an interface and was never run against the live API (no key, no spend).

## What I reviewed, changed and can explain

> **Fill this section in yourself, honestly, before submitting.** The follow-up review asks you to explain the code
> and change it live. Only list what is true.

* Files / modules I read line by line and can explain: `<e.g. app/ingestion/service.py, app/detection/rules.py ...>`
* Things I changed myself after the AI draft (and why): `<e.g. renamed X, changed the deadline boundary to ..., added test Y>`
* Behaviours I verified by running them myself: `<e.g. imported fixtures twice, ran detection, completed a task twice with the same Idempotency-Key>`
* Parts I did **not** fully review (so I would not claim them): `<list, or "none">`
* Local test result on my machine: `<paste the last line of  python -m pytest -q>`

## Where AI is deliberately kept out of the product

The detector, unit conversion, eligibility and permissions are deterministic code. The optional AI feature may only
explain a finding and suggest investigation steps: it cannot produce quantities, change statuses, assign people or
mark anything verified, and its output is schema-checked and citation-checked, with a deterministic fallback
(`app/ai/summary.py`, `tests/test_ai.py`).