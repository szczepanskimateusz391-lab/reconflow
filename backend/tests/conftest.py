import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("AMOUNT_TOLERANCE", "0.01")
os.environ.setdefault("DOCUMENT_GRACE_DAYS", "3")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.pool import StaticPool

from app import db as db_module
from app.db import Base, get_db
from app.main import app


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def session():
    engine = db_module.create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    local_session = db_module.sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = local_session()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture()
def client(session):
    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def import_demo(client: TestClient) -> None:
    for dataset in ("orders", "payments", "documents", "returns"):
        path = ROOT / "demo-data" / f"{dataset}.csv"
        response = client.post(
            f"/api/imports/{dataset}",
            files={"file": (path.name, path.read_bytes(), "text/csv")},
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "completed", response.json()
