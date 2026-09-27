from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app.db import get_db
from app.main import app


def test_health_reports_available_api_and_database(client):
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "api": "available",
        "database": "available",
        "test_instance": False,
    }


def test_health_distinguishes_database_failure_from_api_failure():
    class UnavailableDatabase:
        def execute(self, _statement):
            raise SQLAlchemyError("database unavailable")

    def override_db():
        yield UnavailableDatabase()

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/health")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "api": "available",
        "database": "unavailable",
        "test_instance": False,
    }
