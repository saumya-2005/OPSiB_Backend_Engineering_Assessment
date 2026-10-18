"""Load every fixture CSV in dependency order.

    python -m app.ingestion.cli data/fixtures
"""
import sys
from pathlib import Path

from app.db.session import SessionLocal
from app.ingestion.datasets import LOAD_ORDER
from app.ingestion.service import run_import


def load_directory(directory: Path, session) -> list:
    results = []
    for dataset in LOAD_ORDER:
        path = directory / f"{dataset}.csv"
        results.append(run_import(session, dataset, path.read_bytes(), path.name))
    return results


if __name__ == "__main__":
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "data/fixtures")
    with SessionLocal() as session:
        for b in load_directory(folder, session):
            print(f"{b.dataset:22} {b.status:22} total={b.total_rows} accepted={b.accepted_rows} "
                  f"rejected={b.rejected_rows} duplicate={b.duplicate_rows} "
                  f"conflict={b.conflict_rows} updated={b.updated_rows}")
