"""Run detection from the command line and print one line per PO line.

    python -m app.detection.cli
"""
from app import clock
from app.db.session import SessionLocal
from app.detection.service import run_detection

if __name__ == "__main__":
    print(f"evaluation time: {clock.now().isoformat()}")
    with SessionLocal() as session:
        for r in run_detection(session):
            qty = "" if r.expected is None else (
                f"expected={r.expected.normalize():f} received={'-' if r.received is None else f'{r.received.normalize():f}'}"
                f" variance={'-' if r.variance is None else f'{r.variance.normalize():f}'}")
            print(f"{r.po_number}-L{r.line_number}  {r.outcome:24} {r.case_action or '-':11} {qty}")
