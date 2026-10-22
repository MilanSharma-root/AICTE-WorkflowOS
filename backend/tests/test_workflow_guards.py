from fastapi.testclient import TestClient
from app.main import app

DEMO = {"username": "admin@demo.local", "password": "Demo@12345"}

def auth(client):
    response = client.post("/api/v1/auth/login", json=DEMO)
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["access_token"]}

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
        headers = auth(client)
        response = client.get("/api/v1/dashboard/summary", headers=headers)
        assert response.status_code == 200
        assert "total_applications" in response.json()
        assert "stage_counts" in response.json()

def test_workflow_stage_configuration_is_available_to_authorized_user():
    with TestClient(app) as client:
        response = client.get("/api/v1/workflow/stages", headers=auth(client))
        assert response.status_code == 200
        assert len(response.json()["stages"]) >= 2

def test_transition_endpoint_rejects_unknown_stage():
    with TestClient(app) as client:
        headers = auth(client)
        apps = client.get("/api/v1/applications", headers=headers)
        assert apps.status_code == 200
        if not apps.json():
            return
        app_id = apps.json()[0]["id"]
        response = client.post(
            f"/api/v1/applications/{app_id}/transition",
            headers=headers,
            json={"to_stage": "NOT_A_REAL_STAGE", "status": "In Review", "note": "test"},
        )
        assert response.status_code == 422

def test_return_transition_requires_a_reason():
    with TestClient(app) as client:
        headers = auth(client)
        apps = client.get("/api/v1/applications", headers=headers)
        assert apps.status_code == 200
        candidate = next((item for item in apps.json() if item["status"] not in {"Completed", "Rejected"} and item["current_stage"] not in {"Approved / Closed", "Rejected"}), None)
        if candidate is None:
            return
        response = client.post(
            f"/api/v1/applications/{candidate['id']}/transition",
            headers=headers,
            json={"to_stage": "Returned to Institution", "status": "Returned", "note": ""},
        )
        assert response.status_code == 422

def test_completed_status_cannot_be_set_at_a_nonterminal_stage():
    with TestClient(app) as client:
        headers = auth(client)
        response = client.get("/api/v1/applications", headers=headers)
        assert response.status_code == 200
        candidate = next((item for item in response.json() if item["status"] not in {"Completed", "Rejected"} and item["current_stage"] not in {"Approved / Closed", "Rejected"}), None)
        if candidate is None:
            return
        result = client.post(
            f"/api/v1/applications/{candidate['id']}/transition",
            headers=headers,
            json={"to_stage": "Scrutiny", "status": "Completed", "note": "invalid transition test"},
        )
        assert result.status_code == 422

def test_application_type_route_requires_admin_and_validates_stages():
    with TestClient(app) as client:
        admin_headers = auth(client)
        officer_login = client.post("/api/v1/auth/login", json={"username": "officer@demo.local", "password": "Demo@12345"})
        assert officer_login.status_code == 200
        officer_headers = {"Authorization": "Bearer " + officer_login.json()["access_token"]}
        payload = {"application_type": "Demo Custom Type", "stages": ["Application Intake", "Scrutiny", "Approved / Closed", "Rejected"]}
        assert client.put("/api/v1/workflow/routes", headers=officer_headers, json=payload).status_code == 403
        invalid = {"application_type": "Demo Custom Type", "stages": ["Application Intake", "Made Up Stage"]}
        assert client.put("/api/v1/workflow/routes", headers=admin_headers, json=invalid).status_code == 422
        result = client.put("/api/v1/workflow/routes", headers=admin_headers, json=payload)
        assert result.status_code == 200
        assert result.json()["application_type"] == "Demo Custom Type"

def test_application_type_route_restricts_transition_stages():
    with TestClient(app) as client:
        headers = auth(client)
        created = client.post("/api/v1/applications", headers=headers, json={
            "institution_name": "Route Test Institution", "application_type": "Route Restriction Test",
            "current_stage": "Application Intake", "priority": "Normal"
        })
        assert created.status_code == 201
        app_id = created.json()["id"]
        route = {"application_type": "Route Restriction Test", "stages": ["Application Intake", "Scrutiny", "Approved / Closed", "Rejected"]}
        assert client.put("/api/v1/workflow/routes", headers=headers, json=route).status_code == 200
        result = client.post(f"/api/v1/applications/{app_id}/transition", headers=headers, json={
            "to_stage": "EVC Visit", "status": "In Review", "note": "route guard test"
        })
        assert result.status_code == 422
