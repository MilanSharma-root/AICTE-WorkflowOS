from fastapi.testclient import TestClient
from app.main import app

def test_health_route_exists():
    # A database must be available before running the app lifespan in this smoke test.
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
