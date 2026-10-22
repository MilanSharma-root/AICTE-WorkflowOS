from fastapi.testclient import TestClient
from app.main import app

def test_health_route_exists():
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

def test_application_api_requires_authentication():
    with TestClient(app) as client:
        response = client.get("/api/v1/applications")
        assert response.status_code == 401

def test_demo_login_and_dashboard_summary():
    with TestClient(app) as client:
        login = client.post("/api/v1/auth/login", json={"username": "admin@demo.local", "password": "Demo@12345"})
        assert login.status_code == 200
        token = login.json()["access_token"]
        response = client.get("/api/v1/dashboard/summary", headers={"Authorization": "Bearer " + token})
        assert response.status_code == 200
        assert "total_applications" in response.json()
        assert "stage_counts" in response.json()
