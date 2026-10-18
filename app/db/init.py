"""Dev helper: create tables.  python -m app.db.init [--reset]
(Alembic migrations are added in the final step; this is the quick path.)"""
import sys

from app.db import models  # noqa: F401  (registers tables)
from app.db.session import Base, engine

if __name__ == "__main__":
    if "--reset" in sys.argv:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    print("tables:", ", ".join(sorted(Base.metadata.tables)))
