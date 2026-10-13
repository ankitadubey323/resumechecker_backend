import os

os.environ["ACCESS_TOKEN_SECRET"] = "test-access-secret-" + "a" * 40
os.environ["REFRESH_TOKEN_SECRET"] = "test-refresh-secret-" + "b" * 40
os.environ["BCRYPT_ROUNDS"] = "4"  # bcrypt's minimum; keeps the suite fast
os.environ["MONGO_URI"] = "mongodb://unused"  # replaced by mongomock below
os.environ["MONGO_DATABASE"] = "resume_customizer_test"
os.environ["ENVIRONMENT"] = "development"

import mongomock  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import database  # noqa: E402
from app.main import app  # noqa: E402
from app.rate_limit import limiter  # noqa: E402


@pytest.fixture
def db(monkeypatch):
    """A fresh in-memory MongoDB for each test."""
    monkeypatch.setattr(database, "_client", mongomock.MongoClient(tz_aware=True))
    return database.get_db()


@pytest.fixture
def client(db):
    limiter.reset()
    with TestClient(app) as c:
        yield c
