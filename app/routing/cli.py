"""Create and route tasks from the command line:  python -m app.routing.cli"""
from app.db.session import SessionLocal
from app.routing.service import generate_tasks

if __name__ == "__main__":
    with SessionLocal() as session:
        for g in generate_tasks(session):
            print(f"{g.case_key:32} {g.action:16} {g.status:10} assignee={g.assignee_id}")
