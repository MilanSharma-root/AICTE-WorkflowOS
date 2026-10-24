from io import BytesIO

from fastapi.testclient import TestClient
from pypdf import PdfWriter

from app.main import app


def _blank_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _login(client: TestClient, username: str, password: str = "Demo@12345") -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def test_college_submission_requires_all_three_documents_and_creates_private_tracking():
    with TestClient(app) as client:
        data = {
            "institution_name": "College Intake Integration Test",
            "institution_type": "Technical Institution",
            "state": "Rajasthan",
            "application_type": "New Institution",
            "email": "college-intake-integration@example.org",
            "password": "TestCollege@123",
        }
        files = {
            "registration_document": ("registration.pdf", _blank_pdf(), "application/pdf"),
            "land_building_document": ("land.pdf", _blank_pdf(), "application/pdf"),
            "faculty_infrastructure_document": ("faculty.pdf", _blank_pdf(), "application/pdf"),
        }
        missing = client.post("/api/v1/public/college-submissions", data=data, files={
            "registration_document": files["registration_document"],
            "land_building_document": files["land_building_document"],
        })
        assert missing.status_code == 422

        submitted = client.post("/api/v1/public/college-submissions", data=data, files=files)
        assert submitted.status_code == 201
        body = submitted.json()
        assert body["document_count"] == 3
        assert len(body["document_checks"]) == 3
        assert body["application_no"]

        college_headers = _login(client, data["email"], data["password"])
        tracking = client.get("/api/v1/college/application", headers=college_headers)
        assert tracking.status_code == 200
        assert tracking.json()["application"]["application_no"] == body["application_no"]
        assert len(tracking.json()["documents"]) == 3

        admin_headers = _login(client, "admin@demo.local")
        register = client.get("/api/v1/applications", headers=admin_headers)
        assert register.status_code == 200
        assert any(row["application_no"] == body["application_no"] for row in register.json())


def test_authorised_stage_completion_advances_and_other_stage_role_is_blocked():
    with TestClient(app) as client:
        admin_headers = _login(client, "admin@demo.local")
        created = client.post("/api/v1/applications", headers=admin_headers, json={
            "institution_name": "Stage Advance Integration Test",
            "application_type": "Extension of Approval",
            "current_stage": "Application Intake",
            "priority": "Normal",
        })
        assert created.status_code == 201
        application_id = created.json()["id"]

        evc_headers = _login(client, "evc@demo.local")
        forbidden = client.post(f"/api/v1/applications/{application_id}/transition", headers=evc_headers, json={
            "to_stage": "Application Intake", "status": "Stage Completed", "note": "wrong role",
        })
        assert forbidden.status_code == 403

        scrutiny_headers = _login(client, "scrutiny@demo.local")
        advanced = client.post(f"/api/v1/applications/{application_id}/transition", headers=scrutiny_headers, json={
            "to_stage": "Application Intake", "status": "Stage Completed", "note": "Intake checks completed",
        })
        assert advanced.status_code == 200
        assert advanced.json()["current_stage"] == "Scrutiny"
        assert advanced.json()["status"] == "In Review"
