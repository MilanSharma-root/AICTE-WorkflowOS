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


def test_automation_status_and_manual_rule_run_are_available_to_authorized_roles():
    with TestClient(app) as client:
        headers = auth(client)
        status = client.get("/api/v1/automation/status", headers=headers)
        assert status.status_code == 200
        assert "unassigned_applications" in status.json()
        result = client.post("/api/v1/automation/run", headers=headers, json={})
        assert result.status_code == 200
        body = result.json()
        assert "scanned" in body and "alerts_created" in body
        assert "no approval decision" in body["message"]

def test_automation_run_is_restricted_to_admin_and_officer():
    with TestClient(app) as client:
        login_response = client.post("/api/v1/auth/login", json={"username": "auditor@demo.local", "password": "Demo@12345"})
        assert login_response.status_code == 200
        headers = {"Authorization": "Bearer " + login_response.json()["access_token"]}
        assert client.post("/api/v1/automation/run", headers=headers, json={}).status_code == 403


def test_escalation_policy_exposes_tiered_follow_up_rules():
    with TestClient(app) as client:
        response = client.get("/api/v1/automation/escalation-policy", headers=auth(client))
        assert response.status_code == 200
        levels = response.json()["levels"]
        assert [item["level"] for item in levels] == [1, 2, 3]
        assert "prototype defaults" in response.json()["note"]


def test_document_checklist_template_apply_and_pre_scrutiny_summary():
    with TestClient(app) as client:
        headers = auth(client)
        app_response = client.post("/api/v1/applications", headers=headers, json={
            "institution_name": "Checklist Test Institution", "application_type": "Checklist Test Type",
            "current_stage": "Application Intake", "priority": "Normal"
        })
        assert app_response.status_code == 201
        app_id = app_response.json()["id"]
        template = client.put("/api/v1/document-checklists/templates", headers=headers, json={
            "application_type": "Checklist Test Type",
            "items": [
                {"key": "form-a", "label": "Application form", "required": True},
                {"key": "optional-b", "label": "Supporting note", "required": False}
            ]
        })
        assert template.status_code == 200
        applied = client.post(f"/api/v1/applications/{app_id}/document-checklist/apply", headers=headers)
        assert applied.status_code == 200
        assert applied.json()["items_added"] == 2
        checklist = client.get(f"/api/v1/applications/{app_id}/document-checklist", headers=headers)
        assert checklist.status_code == 200
        assert checklist.json()["summary"]["missing_required"] == 1
        assert checklist.json()["summary"]["ready_for_human_review"] is False
        run = client.post("/api/v1/automation/run", headers=headers, json={})
        assert run.status_code == 200
        alerts = client.get("/api/v1/alerts", headers=headers)
        assert any("required checklist items outstanding" in item["title"] for item in alerts.json())
        # Applying the same template twice must not duplicate existing checklist rows.
        again = client.post(f"/api/v1/applications/{app_id}/document-checklist/apply", headers=headers)
        assert again.status_code == 200
        assert again.json()["items_added"] == 0

def test_document_checklist_template_is_admin_only():
    with TestClient(app) as client:
        login_response = client.post("/api/v1/auth/login", json={"username": "officer@demo.local", "password": "Demo@12345"})
        assert login_response.status_code == 200
        headers = {"Authorization": "Bearer " + login_response.json()["access_token"]}
        result = client.put("/api/v1/document-checklists/templates", headers=headers, json={
            "application_type": "Restricted Checklist Type",
            "items": [{"key": "one", "label": "Document one", "required": True}]
        })
        assert result.status_code == 403

def test_transition_must_follow_next_configured_stage():
    with TestClient(app) as client:
        headers = auth(client)
        created = client.post("/api/v1/applications", headers=headers, json={
            "institution_name": "Sequential Route Test", "application_type": "Extension of Approval",
            "current_stage": "Application Intake", "priority": "Normal"
        })
        assert created.status_code == 201
        app_id = created.json()["id"]
        skipped = client.post(f"/api/v1/applications/{app_id}/transition", headers=headers, json={
            "to_stage": "EVC Visit", "status": "In Review", "note": "attempt to skip stages"
        })
        assert skipped.status_code == 422
        next_stage = client.post(f"/api/v1/applications/{app_id}/transition", headers=headers, json={
            "to_stage": "Scrutiny", "status": "In Review", "note": "normal progression"
        })
        assert next_stage.status_code == 200
        assert next_stage.json()["current_stage"] == "Scrutiny"


def test_approved_closed_requires_completed_status_and_final_operational_stage():
    with TestClient(app) as client:
        headers = auth(client)
        created = client.post("/api/v1/applications", headers=headers, json={
            "institution_name": "Closure Guard Test", "application_type": "Extension of Approval",
            "current_stage": "Compliance", "priority": "Normal"
        })
        assert created.status_code == 201
        app_id = created.json()["id"]
        invalid = client.post(f"/api/v1/applications/{app_id}/transition", headers=headers, json={
            "to_stage": "Approved / Closed", "status": "In Review", "note": "status mismatch"
        })
        assert invalid.status_code == 422
        valid = client.post(f"/api/v1/applications/{app_id}/transition", headers=headers, json={
            "to_stage": "Approved / Closed", "status": "Completed", "note": "authorised human decision recorded"
        })
        assert valid.status_code == 200
        assert valid.json()["status"] == "Completed"

def test_returned_application_resumes_at_rescrutiny_then_rejoins_route():
    with TestClient(app) as client:
        headers = auth(client)
        created = client.post("/api/v1/applications", headers=headers, json={
            "institution_name": "Return Cycle Test", "application_type": "Extension of Approval",
            "current_stage": "Scrutiny", "priority": "Normal"
        })
        assert created.status_code == 201
        app_id = created.json()["id"]
        returned = client.post(f"/api/v1/applications/{app_id}/transition", headers=headers, json={
            "to_stage": "Returned to Institution", "status": "Returned", "note": "Please supply missing evidence."
        })
        assert returned.status_code == 200
        rescrutiny = client.post(f"/api/v1/applications/{app_id}/transition", headers=headers, json={
            "to_stage": "Re-scrutiny", "status": "In Review", "note": "Updated evidence received."
        })
        assert rescrutiny.status_code == 200
        resumed = client.post(f"/api/v1/applications/{app_id}/transition", headers=headers, json={
            "to_stage": "EVC Scheduling", "status": "In Review", "note": "Scrutiny recheck complete."
        })
        assert resumed.status_code == 200
        assert resumed.json()["current_stage"] == "EVC Scheduling"


def test_document_validity_lifecycle_and_date_validation():
    with TestClient(app) as client:
        headers = auth(client)
        created = client.post("/api/v1/applications", headers=headers, json={
            "institution_name": "Validity Test Institution", "application_type": "Extension of Approval"
        })
        assert created.status_code == 201
        app_id = created.json()["id"]

        invalid = client.post("/api/v1/document-validity", headers=headers, json={
            "application_id": app_id, "document_name": "Lease deed",
            "issued_on": "2026-10-20T00:00:00Z", "expiry_date": "2026-10-01T00:00:00Z"
        })
        assert invalid.status_code == 422

        result = client.post("/api/v1/document-validity", headers=headers, json={
            "application_id": app_id, "document_name": "Lease deed",
            "category": "Lease / ownership", "expiry_date": "2026-11-15T00:00:00Z",
            "renewal_due_date": "2026-11-01T00:00:00Z"
        })
        assert result.status_code == 201
        item_id = result.json()["id"]
        assert result.json()["status"] == "Active"

        listed = client.get(f"/api/v1/document-validity?application_id={app_id}", headers=headers)
        assert listed.status_code == 200
        assert any(x["id"] == item_id for x in listed.json())

        patched = client.patch(f"/api/v1/document-validity/{item_id}", headers=headers, json={
            "status": "Under Verification", "notes": "Renewal evidence submitted for review"
        })
        assert patched.status_code == 200
        assert patched.json()["status"] == "Under Verification"

        bad_status = client.patch(f"/api/v1/document-validity/{item_id}", headers=headers, json={"status": "Auto Approved"})
        assert bad_status.status_code == 422

        expired = client.post("/api/v1/document-validity", headers=headers, json={
            "application_id": app_id, "document_name": "Expired occupancy certificate",
            "category": "Occupancy certificate", "expiry_date": "2026-10-01T00:00:00Z"
        })
        assert expired.status_code == 201
        alerts = client.get("/api/v1/alerts", headers=headers)
        assert alerts.status_code == 200
        assert any(x["category"] == "Document Validity" and "Expired occupancy certificate" in x["title"] for x in alerts.json())

        audit_rows = client.get("/api/v1/audit", headers=headers)
        assert audit_rows.status_code == 200
        assert any(x["action"] == "DOCUMENT_VALIDITY_UPDATED" for x in audit_rows.json())


def test_document_validity_links_only_evidence_from_same_application():
    with TestClient(app) as client:
        headers = auth(client)
        first = client.post("/api/v1/applications", headers=headers, json={
            "institution_name": "Evidence Link Institution A", "application_type": "Extension of Approval"
        })
        second = client.post("/api/v1/applications", headers=headers, json={
            "institution_name": "Evidence Link Institution B", "application_type": "Extension of Approval"
        })
        assert first.status_code == 201 and second.status_code == 201
        first_id, second_id = first.json()["id"], second.json()["id"]

        uploaded = client.post(
            f"/api/v1/applications/{first_id}/documents",
            headers=headers,
            files={"file": ("lease.pdf", b"%PDF-1.4 test fixture", "application/pdf")},
        )
        assert uploaded.status_code == 201
        document_id = uploaded.json()["id"]

        linked = client.post("/api/v1/document-validity", headers=headers, json={
            "application_id": first_id, "document_name": "Lease deed", "document_id": document_id
        })
        assert linked.status_code == 201
        assert linked.json()["document_id"] == document_id

        mismatch = client.post("/api/v1/document-validity", headers=headers, json={
            "application_id": second_id, "document_name": "Wrong application link", "document_id": document_id
        })
        assert mismatch.status_code == 422


def test_production_configuration_rejects_default_or_weak_secret():
    from pydantic import ValidationError
    from app.config import Settings

    try:
        Settings(app_env="production", secret_key="CHANGE_THIS_SECRET_FOR_PRODUCTION")
        assert False, "Production must reject the default secret"
    except ValidationError:
        pass

    try:
        Settings(app_env="production", secret_key="short-secret")
        assert False, "Production must reject weak secrets"
    except ValidationError:
        pass

    secure = Settings(
        app_env="production",
        secret_key="a-long-random-secret-key-for-production-at-least-32",
        cors_origins="https://workflow.example.org",
    )
    assert secure.app_env == "production"


def test_automation_routes_unassigned_case_to_authorised_stage_owner():
    with TestClient(app) as client:
        created = client.post("/api/v1/public/applications", json={
            "institution_name": "Automation Routing Integration Test",
            "application_type": "Extension of Approval",
            "current_stage": "Application Intake",
            "priority": "Normal",
            "state": "Uttar Pradesh",
        })
        assert created.status_code == 201
        app_id = created.json()["id"]
        assert created.json()["assigned_to"] is None

        admin_headers = auth(client)
        run = client.post("/api/v1/automation/run", headers=admin_headers, json={})
        assert run.status_code == 200
        updated = client.get(f"/api/v1/applications/{app_id}", headers=admin_headers)
        assert updated.status_code == 200
        assert updated.json()["assigned_to"] == "Scrutiny Officer"

        history = client.get(f"/api/v1/applications/{app_id}/history", headers=admin_headers)
        assert history.status_code == 200
        # Assignment is auditable even though no statutory workflow stage is advanced.
        audit_rows = client.get("/api/v1/audit?limit=500", headers=admin_headers)
        assert audit_rows.status_code == 200
        assert any(row["application_id"] == app_id and row["action"] == "AUTOMATION_CASE_ASSIGNED" for row in audit_rows.json())

def test_authorised_transition_automatically_hands_case_to_next_stage_owner():
    with TestClient(app) as client:
        headers = auth(client)
        created = client.post("/api/v1/applications", headers=headers, json={
            "institution_name": "Automatic Handoff Test College",
            "application_type": "Extension of Approval",
            "current_stage": "Application Intake",
            "priority": "Normal",
        })
        assert created.status_code == 201
        app_id = created.json()["id"]

        transitioned = client.post(f"/api/v1/applications/{app_id}/transition", headers=headers, json={
            "to_stage": "Scrutiny",
            "status": "In Review",
            "note": "Intake checks recorded; hand off to scrutiny.",
        })
        assert transitioned.status_code == 200
        assert transitioned.json()["current_stage"] == "Scrutiny"
        assert transitioned.json()["assigned_to"] == "Scrutiny Reviewer" or transitioned.json()["assigned_to"] == "Scrutiny Officer"

        alerts = client.get("/api/v1/alerts", headers=headers)
        assert alerts.status_code == 200
        assert any(
            alert["application_id"] == app_id
            and alert["category"] == "Workflow Handoff"
            and alert["title"] == "Application assigned to your stage"
            for alert in alerts.json()
        )
