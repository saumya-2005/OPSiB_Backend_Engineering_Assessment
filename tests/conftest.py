from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db import models  # noqa: F401
from app.db.session import Base, get_session, make_engine
from app.main import app

FIXTURES = Path(__file__).resolve().parent.parent / "data" / "fixtures"


@pytest.fixture(scope="session")
def engine():
    eng = make_engine(get_settings().test_database_url)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine):
    """Fresh empty schema for every test (tests never touch the dev DB)."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as s:
        yield s


@pytest.fixture()
def client(engine, session):
    maker = sessionmaker(bind=engine, expire_on_commit=False)

    def _override():
        with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _override
    yield TestClient(app)
    app.dependency_overrides.clear()


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / f"{name}.csv").read_bytes()


@pytest.fixture()
def load_all(session):
    from app.ingestion.cli import load_directory
    return lambda: load_directory(FIXTURES, session)
