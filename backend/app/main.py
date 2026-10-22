import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
import csv, io, json
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.config import settings
from app.database import Base, engine, get_db, SessionLocal
from app.models import (Alert, Application, AuditEvent, ComplianceItem, DocumentRecord,
                        EVCVisit, Undertaking, User, WorkflowEvent, WorkflowConfiguration, ApplicationWorkflowRoute, DocumentChecklistTemplate, ApplicationDocumentCheck, DocumentValidity)
from app.schemas import (ApplicationCreate, ApplicationOut, ApplicationUpdate, ComplianceCreate,
                         ComplianceUpdate, EVCVisitCreate, EVCVisitUpdate, LoginIn,
                         TransitionIn, UndertakingCreate, UndertakingUpdate, WorkflowConfigUpdate, ApplicationWorkflowRouteUpdate, DocumentChecklistTemplateUpdate, DocumentCheckUpdate, DocumentValidityCreate, DocumentValidityUpdate)
from app.security import create_access_token, hash_password, require_roles, verify_password

WORKFLOW_STAGES = [
    "Application Intake", "Scrutiny", "Re-scrutiny", "EVC Scheduling",
    "EVC Visit", "SAC/SHC Review", "Compliance", "Approved / Closed",
    "Returned to Institution", "Rejected"
]
TERMINAL_STAGES = {"Approved / Closed", "Rejected"}

def get_workflow_stages(db: Session) -> list[str]:
    config = db.scalar(select(WorkflowConfiguration).where(WorkflowConfiguration.active.is_(True)).order_by(WorkflowConfiguration.updated_at.desc()))
    if config:
        try:
            stages = json.loads(config.stages_json)
            if isinstance(stages, list) and len(stages) >= 2:
                return [str(stage) for stage in stages]
        except (ValueError, TypeError):
            pass
    return WORKFLOW_STAGES

def stages_for_application(db: Session, application_type: str) -> list[str]:
    route = db.scalar(select(ApplicationWorkflowRoute).where(
        ApplicationWorkflowRoute.application_type == application_type,
        ApplicationWorkflowRoute.active.is_(True)
    ))
    if route:
        try:
            stages = json.loads(route.stages_json)
            if isinstance(stages, list) and len(stages) >= 2:
                return [str(stage) for stage in stages]
        except (ValueError, TypeError):
            pass
    return get_workflow_stages(db)

def sync_deadline_alerts(db: Session):
    now = utcnow()
    for item in db.scalars(select(Application).where(Application.status.not_in(["Completed", "Rejected"]))).all():
        if not item.due_date:
            continue
        days = (safe_dt(item.due_date) - now).total_seconds() / 86400
        title = "Overdue workflow item" if days < 0 else ("Deadline approaching" if days <= 3 else None)
        if title and not db.scalar(select(Alert.id).where(Alert.application_id == item.id, Alert.category == "Deadline", Alert.title == title)):
            due_text = "target date has passed" if days < 0 else "action due in " + str(max(0, int(days))) + " day(s)"
            db.add(Alert(application_id=item.id, title=title, message=f"{item.institution_name}: {due_text}. Review and escalate where appropriate.", severity="Critical" if days < 0 else "High", category="Deadline"))
        # Escalation alerts are level-specific and deduplicated independently.
        if days < 0:
            overdue_days = int(abs(days))
            level = 3 if overdue_days >= 7 else (2 if overdue_days >= 3 else 1)
            level_title = f"Escalation L{level}: overdue application"
            if not db.scalar(select(Alert.id).where(Alert.application_id == item.id, Alert.category == "Escalation", Alert.title == level_title)):
                db.add(Alert(application_id=item.id, title=level_title, message=f"{item.application_no} is {overdue_days} day(s) overdue. Level {level} follow-up is due; notify/refer to the designated authority according to locally approved escalation policy.", severity="Critical" if level >= 2 else "High", category="Escalation"))
    for item in db.scalars(select(ComplianceItem).where(ComplianceItem.status.not_in(["Verified", "Submitted"]))).all():
        if not item.due_date:
            continue
        days = (safe_dt(item.due_date) - now).total_seconds() / 86400
        if days <= 3:
            title = "Compliance deadline: " + item.title
            if not db.scalar(select(Alert.id).where(Alert.application_id == item.application_id, Alert.category == "Compliance", Alert.title == title)):
                db.add(Alert(application_id=item.application_id, title=title, message=f"Compliance item '{item.title}' is {'overdue' if days < 0 else 'due soon'}.", severity="Critical" if days < 0 else "High", category="Compliance"))
    for item in db.scalars(select(Undertaking).where(Undertaking.submitted.is_(False))).all():
        if not item.due_date:
            continue
        days = (safe_dt(item.due_date) - now).total_seconds() / 86400
        if days <= 3:
            title = "Undertaking deadline: " + item.title
            if not db.scalar(select(Alert.id).where(Alert.application_id == item.application_id, Alert.category == "Undertaking", Alert.title == title)):
                db.add(Alert(application_id=item.application_id, title=title, message=f"Undertaking '{item.title}' is {'overdue' if days < 0 else 'due soon'}.", severity="Critical" if days < 0 else "High", category="Undertaking"))
    for item in db.scalars(select(DocumentValidity).where(DocumentValidity.status.not_in(["Renewed", "Not Applicable"]))).all():
        if not item.expiry_date:
            continue
        days = (safe_dt(item.expiry_date) - now).total_seconds() / 86400
        if days > 30:
            continue
        state = "expired" if days < 0 else ("expires within 7 days" if days <= 7 else "expires within 30 days")
        title = f"Document validity: {item.document_name} {state}"
        if not db.scalar(select(Alert.id).where(Alert.application_id == item.application_id, Alert.category == "Document Validity", Alert.title == title)):
            severity = "Critical" if days < 0 else ("High" if days <= 7 else "Medium")
            db.add(Alert(application_id=item.application_id, title=title,
                         message=f"{item.document_name} ({item.category}) {state}. Confirm the underlying record, applicable requirements and renewal action with an authorised reviewer.",
                         severity=severity, category="Document Validity"))
    for item in db.scalars(select(EVCVisit).where(EVCVisit.status.in_(["Proposed", "Scheduled"]))).all():
        if not item.proposed_date:
            continue
        days = (safe_dt(item.proposed_date) - now).total_seconds() / 86400
        if days <= 3:
            title = "EVC visit approaching"
            if not db.scalar(select(Alert.id).where(Alert.application_id == item.application_id, Alert.category == "EVC", Alert.title == title)):
                db.add(Alert(application_id=item.application_id, title=title, message=f"EVC visit for application #{item.application_id} is {'overdue' if days < 0 else 'scheduled soon'}. Confirm panel and logistics.", severity="Critical" if days < 0 else "High", category="EVC"))
    db.commit()

def run_deadline_sync():
    db = SessionLocal()
    try:
        sync_deadline_alerts(db)
    finally:
        db.close()

async def deadline_scheduler():
    # A local in-process scheduler for the prototype. Production should use a durable worker.
    while True:
        await asyncio.sleep(15 * 60)
        try:
            await asyncio.to_thread(run_deadline_sync)
        except Exception:
            # Keep the API alive; the next interval will retry.
            continue
ALLOWED_ROLES = {"admin", "officer", "reviewer", "auditor"}

def utcnow():
    return datetime.now(timezone.utc)

def safe_dt(value):
    if value is None: return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

def audit(db: Session, action: str, actor: str, application_id: int | None = None, details: str | None = None):
    db.add(AuditEvent(application_id=application_id, action=action, actor=actor, details=details))

def seed_demo_data(db: Session):
    if not db.scalar(select(func.count()).select_from(User)):
        demo_users = [
            ("admin@demo.local", "Demo Administrator", "admin"),
            ("officer@demo.local", "Case Officer", "officer"),
            ("reviewer@demo.local", "Scrutiny Reviewer", "reviewer"),
            ("auditor@demo.local", "Audit Viewer", "auditor"),
        ]
        for username, full_name, role in demo_users:
            db.add(User(username=username, full_name=full_name, role=role, password_hash=hash_password("Demo@12345")))
    if db.scalar(select(func.count()).select_from(Application)):
        db.commit()
        return
    now = utcnow()
    sample = [
        ("North Valley Institute of Technology", "Uttar Pradesh", "Extension of Approval", "Scrutiny", "High", 2),
        ("Saraswati College of Engineering", "Rajasthan", "New Institution", "EVC Scheduling", "High", 5),
        ("Eastern Institute of Management", "Bihar", "Increase in Intake", "SAC/SHC Review", "Normal", 8),
        ("Riverbend Technical Campus", "Madhya Pradesh", "Extension of Approval", "Compliance", "Normal", -2),
        ("Central Polytechnic Institute", "Uttarakhand", "New Course", "Application Intake", "Low", 12),
        ("Pioneer School of Applied Sciences", "Haryana", "New Institution", "Re-scrutiny", "High", 1),
    ]
    for i, (name, state, app_type, stage, priority, days) in enumerate(sample, start=1):
        item = Application(
            application_no=f"AICTE-{now.year}-{i:06d}", institution_name=name, state=state,
            application_type=app_type, current_stage=stage,
            status="Completed" if stage == "Approved / Closed" else ("In Review" if stage in {"EVC Scheduling", "SAC/SHC Review", "Re-scrutiny"} else "Pending"),
            priority=priority, assigned_to=["R. Mehta", "A. Sharma", "S. Rao", "K. Singh", "P. Nair", "M. Verma"][i-1],
            submitted_at=now-timedelta(days=20+i*3), due_date=now+timedelta(days=days),
            notes="Synthetic demonstration record; replace with verified institutional data."
        )
        db.add(item)
        db.flush()
        db.add(WorkflowEvent(application_id=item.id, from_stage=None, to_stage=stage, status=item.status, actor="system", note="Seeded synthetic demo record"))
        db.add(AuditEvent(application_id=item.id, action="DEMO_RECORD_CREATED", actor="system", details="Synthetic seed data"))
        if i in (1, 4, 5):
            db.add(ComplianceItem(application_id=item.id, title=["Scrutiny evidence verification", "Lease deed validity check", "Faculty undertaking"][ [1,4,5].index(i) ],
                                  category="Document validity" if i == 4 else "Undertaking",
                                  due_date=now+timedelta(days=days), status="Pending", assigned_to=item.assigned_to,
                                  notes="Demo checklist item; confirm official requirements."))
        if i == 2:
            db.add(EVCVisit(application_id=item.id, proposed_date=now+timedelta(days=7), location="Institution campus",
                            panel_members="Panel assignment pending", status="Proposed"))
        if days <= 2:
            db.add(Alert(application_id=item.id, title="Deadline approaching", message=f"{item.institution_name}: action due in {days} day(s).", severity="High", category="Deadline"))
        if days < 0:
            db.add(Alert(application_id=item.id, title="Overdue workflow item", message=f"{item.institution_name}: target date has passed; review and escalate if required.", severity="Critical", category="Escalation"))
    db.commit()

@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_demo_data(db)
        if not db.scalar(select(func.count()).select_from(WorkflowConfiguration)):
            db.add(WorkflowConfiguration(name="Default approval route", stages_json=json.dumps(WORKFLOW_STAGES), active=True, updated_by="system"))
            db.commit()
    finally:
        db.close()
    Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
    # Generate due-date alerts on startup and keep checking without waiting for a user to open Alerts.
    await asyncio.to_thread(run_deadline_sync)
    scheduler_task = asyncio.create_task(deadline_scheduler())
    try:
        yield
    finally:
        scheduler_task.cancel()
        with suppress(asyncio.CancelledError):
            await scheduler_task

app = FastAPI(title=settings.app_name, version="1.0.0", description="AICTE workflow prototype. Configure official rules before production.", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=list(dict.fromkeys([*settings.cors_origin_list, "http://localhost:5173", "http://127.0.0.1:5173"])), allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.get("/health")
def health():
    return {"status": "ok", "service": settings.app_name, "database": "configured"}

@app.post("/api/v1/auth/login")
def login(payload: LoginIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == payload.username, User.active.is_(True)))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    return {"access_token": create_access_token(user), "token_type": "bearer", "user": {"username": user.username, "full_name": user.full_name, "role": user.role}}

@app.get("/api/v1/auth/me")
def me(user: User = Depends(require_roles(*ALLOWED_ROLES))):
    return {"username": user.username, "full_name": user.full_name, "role": user.role}

@app.get("/api/v1/workflow/stages")
def workflow_stages(db: Session = Depends(get_db), user: User = Depends(require_roles(*ALLOWED_ROLES))):
    config = db.scalar(select(WorkflowConfiguration).where(WorkflowConfiguration.active.is_(True)).order_by(WorkflowConfiguration.updated_at.desc()))
    return {"name": config.name if config else "Default approval route", "stages": get_workflow_stages(db), "updated_by": config.updated_by if config else "system", "note": "Configure this illustrative pathway against the applicable AICTE Approval Process Handbook and authority-specific routing before operational use."}

@app.put("/api/v1/workflow/config")
def update_workflow_config(payload: WorkflowConfigUpdate, db: Session = Depends(get_db), user: User = Depends(require_roles("admin"))):
    cleaned = [stage.strip() for stage in payload.stages if stage.strip()]
    if len(cleaned) < 2 or len(set(cleaned)) != len(cleaned):
        raise HTTPException(422, "Provide at least two unique, non-empty workflow stages")
    db.query(WorkflowConfiguration).update({WorkflowConfiguration.active: False})
    config = WorkflowConfiguration(name=payload.name, stages_json=json.dumps(cleaned), active=True, updated_by=user.full_name, updated_at=utcnow())
    db.add(config); audit(db, "WORKFLOW_CONFIG_UPDATED", user.full_name, None, " -> ".join(cleaned)); db.commit()
    return {"name": config.name, "stages": cleaned, "updated_by": config.updated_by, "updated_at": config.updated_at, "note": "Review this route with competent authorities before operational use."}

@app.get("/api/v1/workflow/routes")
def list_workflow_routes(db: Session = Depends(get_db), user: User = Depends(require_roles(*ALLOWED_ROLES))):
    rows = db.scalars(select(ApplicationWorkflowRoute).where(ApplicationWorkflowRoute.active.is_(True)).order_by(ApplicationWorkflowRoute.application_type)).all()
    return {
        "default_stages": get_workflow_stages(db),
        "routes": [{"application_type": row.application_type, "stages": json.loads(row.stages_json), "updated_by": row.updated_by, "updated_at": row.updated_at} for row in rows],
        "note": "Illustrative configurable routes only; verify each route against the applicable APH and competent-authority directions."
    }

@app.put("/api/v1/workflow/routes")
def upsert_workflow_route(payload: ApplicationWorkflowRouteUpdate, db: Session = Depends(get_db), user: User = Depends(require_roles("admin"))):
    application_type = payload.application_type.strip()
    cleaned = [stage.strip() for stage in payload.stages if stage.strip()]
    if len(application_type) < 2 or len(cleaned) < 2 or len(set(cleaned)) != len(cleaned):
        raise HTTPException(422, "Provide an application type and at least two unique, non-empty stages")
    unknown = [stage for stage in cleaned if stage not in WORKFLOW_STAGES]
    if unknown:
        raise HTTPException(422, "Stages must use configured canonical stage names: " + ", ".join(unknown))
    if not TERMINAL_STAGES.issubset(set(cleaned)):
        raise HTTPException(422, "Each route must preserve both terminal stages: Approved / Closed and Rejected")
    row = db.scalar(select(ApplicationWorkflowRoute).where(ApplicationWorkflowRoute.application_type == application_type))
    if row:
        row.stages_json = json.dumps(cleaned)
        row.active = True
        row.updated_by = user.full_name
        row.updated_at = utcnow()
    else:
        row = ApplicationWorkflowRoute(application_type=application_type, stages_json=json.dumps(cleaned), active=True, updated_by=user.full_name, updated_at=utcnow())
        db.add(row)
    audit(db, "APPLICATION_WORKFLOW_ROUTE_UPDATED", user.full_name, None, f"{application_type}: " + " -> ".join(cleaned))
    db.commit()
    db.refresh(row)
    return {"application_type": row.application_type, "stages": cleaned, "updated_by": row.updated_by, "updated_at": row.updated_at}

@app.get("/api/v1/automation/escalation-policy")
def escalation_policy(user: User = Depends(require_roles(*ALLOWED_ROLES))):
    return {
        "levels": [
            {"level": 1, "trigger": "1-2 calendar days overdue", "action": "Create in-app follow-up alert"},
            {"level": 2, "trigger": "3-6 calendar days overdue", "action": "Create higher-severity in-app escalation alert"},
            {"level": 3, "trigger": "7 or more calendar days overdue", "action": "Create critical in-app escalation alert"}
        ],
        "note": "Illustrative prototype defaults only. Configure and approve the actual recipient hierarchy, working-day calendar and notification channels before operational use."
    }

@app.get("/api/v1/automation/status")
def automation_status(db: Session = Depends(get_db), user: User = Depends(require_roles(*ALLOWED_ROLES))):
    open_apps = db.scalars(select(Application).where(Application.status.not_in(["Completed", "Rejected"]))).all()
    unassigned = sum(1 for item in open_apps if not (item.assigned_to or "").strip())
    without_documents = sum(1 for item in open_apps if not db.scalar(select(DocumentRecord.id).where(DocumentRecord.application_id == item.id).limit(1)))
    without_due_date = sum(1 for item in open_apps if item.due_date is None)
    overdue = sum(1 for item in open_apps if item.due_date is not None and safe_dt(item.due_date) < utcnow())
    pending_alerts = db.scalar(select(func.count()).select_from(Alert).where(Alert.acknowledged.is_(False))) or 0
    return {
        "enabled": True,
        "scheduler": "running in API process; deadline scan every 15 minutes",
        "open_applications": len(open_apps),
        "unassigned_applications": unassigned,
        "applications_without_document_records": without_documents,
        "applications_without_due_dates": without_due_date,
        "overdue_applications": overdue,
        "unacknowledged_alerts": pending_alerts,
        "capabilities": ["deadline monitoring", "rule-based intake checks", "unassigned-case routing", "audit logging"],
        "limitations": ["document check verifies only whether a file record exists, not content, authenticity or regulatory sufficiency", "notifications are stored in-app; email/SMS delivery is not configured", "statutory decisions remain human-led"]
    }

@app.post("/api/v1/automation/run")
def run_workflow_automation(db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "officer"))):
    # Safe operational automation: assign unowned cases, flag missing intake evidence,
    # and refresh deadline alerts. It never approves/rejects or advances statutory stages.
    open_apps = db.scalars(select(Application).where(Application.status.not_in(["Completed", "Rejected"]))).all()
    assigned = 0
    alerts_created = 0
    checks = []
    for item in open_apps:
        if not (item.assigned_to or "").strip():
            item.assigned_to = user.full_name
            assigned += 1
            audit(db, "AUTOMATION_CASE_ASSIGNED", user.full_name, item.id, "Assigned unowned case to the operator running the automation.")
        has_document = db.scalar(select(DocumentRecord.id).where(DocumentRecord.application_id == item.id).limit(1)) is not None
        if not has_document:
            title = "Pre-scrutiny: no document records"
            exists = db.scalar(select(Alert.id).where(Alert.application_id == item.id, Alert.category == "Pre-scrutiny", Alert.title == title, Alert.acknowledged.is_(False)))
            if not exists:
                db.add(Alert(application_id=item.id, title=title, message=f"{item.application_no}: no uploaded document records were found. Check the applicable official checklist; this is not a finding of non-compliance.", severity="High", category="Pre-scrutiny"))
                alerts_created += 1
        checklist_items = db.scalars(select(ApplicationDocumentCheck).where(ApplicationDocumentCheck.application_id == item.id, ApplicationDocumentCheck.required.is_(True))).all()
        unresolved = [check for check in checklist_items if check.status not in {"Verified", "Not Applicable"}]
        if unresolved:
            title = "Pre-scrutiny: required checklist items outstanding"
            exists = db.scalar(select(Alert.id).where(Alert.application_id == item.id, Alert.category == "Pre-scrutiny", Alert.title == title, Alert.acknowledged.is_(False)))
            if not exists:
                labels = ", ".join(check.label for check in unresolved[:5])
                db.add(Alert(application_id=item.id, title=title, message=f"{item.application_no}: {len(unresolved)} required checklist item(s) are not verified. Review: {labels}. Human scrutiny is required.", severity="High", category="Pre-scrutiny"))
                alerts_created += 1
        if item.due_date is None:
            title = "Workflow target date missing"
            exists = db.scalar(select(Alert.id).where(Alert.application_id == item.id, Alert.category == "Automation", Alert.title == title, Alert.acknowledged.is_(False)))
            if not exists:
                db.add(Alert(application_id=item.id, title=title, message=f"{item.application_no}: set an internal target date after checking the applicable official deadline.", severity="Medium", category="Automation"))
                alerts_created += 1
        checks.append({"application_id": item.id, "application_no": item.application_no, "assigned_to": item.assigned_to, "document_record_found": has_document, "target_date_set": item.due_date is not None})
    sync_deadline_alerts(db)
    audit(db, "WORKFLOW_AUTOMATION_RUN", user.full_name, None, f"Scanned {len(open_apps)} open cases; assigned {assigned}; created {alerts_created} intake alerts.")
    db.commit()
    return {"scanned": len(open_apps), "assigned": assigned, "alerts_created": alerts_created, "checks": checks, "message": "Rule-based checks completed. Review alerts and verify each document checklist manually; no approval decision or workflow-stage change was made."}

@app.get("/api/v1/applications", response_model=list[ApplicationOut])
def list_applications(q: str | None = None, status: str | None = None, stage: str | None = None,
                      db: Session = Depends(get_db), user: User = Depends(require_roles(*ALLOWED_ROLES))):
    stmt = select(Application).order_by(Application.submitted_at.desc())
    if q:
        stmt = stmt.where(Application.institution_name.ilike(f"%{q}%") | Application.application_no.ilike(f"%{q}%") | Application.state.ilike(f"%{q}%"))
    if status and status != "All": stmt = stmt.where(Application.status == status)
    if stage and stage != "All": stmt = stmt.where(Application.current_stage == stage)
    return list(db.scalars(stmt).all())

@app.post("/api/v1/applications", response_model=ApplicationOut, status_code=201)
def create_application(payload: ApplicationCreate, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "officer"))):
    now = utcnow()
    application = Application(application_no=f"AICTE-{now:%Y%m%d%H%M%S}-{uuid4().hex[:4].upper()}",
        institution_name=payload.institution_name, institution_type=payload.institution_type, state=payload.state,
        application_type=payload.application_type, current_stage=payload.current_stage, priority=payload.priority,
        assigned_to=payload.assigned_to or user.full_name, due_date=payload.due_date, notes=payload.notes, status="Pending")
    db.add(application); db.flush()
    db.add(WorkflowEvent(application_id=application.id, from_stage=None, to_stage=application.current_stage, status=application.status, actor=user.full_name, note="Application registered"))
    audit(db, "APPLICATION_CREATED", user.full_name, application.id, f"Created {application.application_no}")
    db.commit(); db.refresh(application)
    return application

@app.get("/api/v1/applications/{application_id}", response_model=ApplicationOut)
def get_application(application_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles(*ALLOWED_ROLES))):
    item = db.get(Application, application_id)
    if not item: raise HTTPException(404, "Application not found")
    return item

@app.patch("/api/v1/applications/{application_id}", response_model=ApplicationOut)
def update_application(application_id: int, payload: ApplicationUpdate, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "officer"))):
    item = db.get(Application, application_id)
    if not item: raise HTTPException(404, "Application not found")
    changes = payload.model_dump(exclude_unset=True)
    for key, value in changes.items(): setattr(item, key, value)
    audit(db, "APPLICATION_UPDATED", user.full_name, item.id, ", ".join(changes.keys()))
    db.commit(); db.refresh(item)
    return item

@app.post("/api/v1/applications/{application_id}/transition", response_model=ApplicationOut)
def transition_application(application_id: int, payload: TransitionIn, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "reviewer"))):
    item = db.get(Application, application_id)
    if not item: raise HTTPException(404, "Application not found")
    stages = stages_for_application(db, item.application_type)
    if payload.to_stage not in stages:
        raise HTTPException(422, "Unknown workflow stage")
    if item.current_stage not in stages:
        raise HTTPException(409, "The current stage is not present in this application's active route. An administrator must reconcile the route before continuing.")
    # Enforce configured route order: normal progression is one stage at a time.
    # Return/reject are explicit review outcomes; returned cases resume at re-scrutiny.
    operational_stages = [stage for stage in stages if stage not in {"Re-scrutiny", "Returned to Institution", "Approved / Closed", "Rejected"}]
    if payload.to_stage not in {"Returned to Institution", "Rejected"}:
        if item.current_stage == "Returned to Institution":
            resume_stage = "Re-scrutiny" if "Re-scrutiny" in stages else ("Scrutiny" if "Scrutiny" in operational_stages else None)
            if payload.to_stage != resume_stage:
                raise HTTPException(422, f"A returned application must resume at {resume_stage or 'the configured scrutiny stage'}.")
        elif item.current_stage == "Re-scrutiny":
            scrutiny_index = operational_stages.index("Scrutiny") if "Scrutiny" in operational_stages else -1
            resume_next = operational_stages[scrutiny_index + 1] if 0 <= scrutiny_index + 1 < len(operational_stages) else None
            if payload.to_stage != resume_next:
                raise HTTPException(422, f"After re-scrutiny, the next configured stage is {resume_next or 'not available; ask an administrator to review the route'}.")
        elif payload.to_stage == "Approved / Closed":
            if item.current_stage not in operational_stages or operational_stages.index(item.current_stage) != len(operational_stages) - 1:
                raise HTTPException(422, "An application can reach Approved / Closed only after completing the configured operational stages.")
        else:
            if item.current_stage not in operational_stages:
                raise HTTPException(422, "This application cannot progress from its current stage through the configured route.")
            current_index = operational_stages.index(item.current_stage)
            if current_index + 1 >= len(operational_stages) or payload.to_stage != operational_stages[current_index + 1]:
                expected = operational_stages[current_index + 1] if current_index + 1 < len(operational_stages) else "Approved / Closed"
                raise HTTPException(422, f"Invalid stage progression. The next configured stage is {expected}.")
    if payload.status not in {"Pending", "In Review", "Returned", "Completed", "Rejected"}:
        raise HTTPException(422, "Unknown workflow status")
    if item.status in {"Completed", "Rejected"} or item.current_stage in TERMINAL_STAGES:
        raise HTTPException(409, "This application is closed; an authorised administrator must reopen it through a separately recorded action.")
    if payload.to_stage in {"Returned to Institution", "Rejected"} or payload.status in {"Returned", "Rejected"}:
        if not (payload.note and payload.note.strip()):
            raise HTTPException(422, "A reason is required when returning or rejecting an application.")
    if payload.status == "Completed" and payload.to_stage != "Approved / Closed":
        raise HTTPException(422, "Completed status is only valid at the Approved / Closed stage.")
    if payload.to_stage == "Approved / Closed" and payload.status != "Completed":
        raise HTTPException(422, "Approved / Closed requires Completed status; record the authorised human decision explicitly.")
    if payload.status == "Rejected" and payload.to_stage != "Rejected":
        raise HTTPException(422, "Rejected status is only valid at the Rejected stage.")
    if payload.status == "Returned" and payload.to_stage != "Returned to Institution":
        raise HTTPException(422, "Returned status is only valid at the Returned to Institution stage.")
    if payload.to_stage == "Approved / Closed" and payload.status not in {"Completed", "In Review", "Pending"}:
        raise HTTPException(422, "Invalid status for closure.")
    old_stage = item.current_stage
    item.current_stage = payload.to_stage
    item.status = "Completed" if payload.to_stage == "Approved / Closed" else ("Rejected" if payload.to_stage == "Rejected" else ("Returned" if payload.to_stage == "Returned to Institution" else payload.status))
    db.add(WorkflowEvent(application_id=item.id, from_stage=old_stage, to_stage=payload.to_stage, status=item.status, note=payload.note, actor=user.full_name))
    audit(db, "WORKFLOW_TRANSITION", user.full_name, item.id, f"{old_stage} -> {payload.to_stage}; status={item.status}; note={payload.note or ''}")
    db.commit(); db.refresh(item)
    return item

@app.get("/api/v1/applications/{application_id}/history")
def application_history(application_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles(*ALLOWED_ROLES))):
    if not db.get(Application, application_id): raise HTTPException(404, "Application not found")
    rows = db.scalars(select(WorkflowEvent).where(WorkflowEvent.application_id == application_id).order_by(WorkflowEvent.created_at.desc())).all()
    return [{"id": r.id, "from_stage": r.from_stage, "to_stage": r.to_stage, "status": r.status, "note": r.note, "actor": r.actor, "created_at": r.created_at} for r in rows]

@app.get("/api/v1/dashboard/summary")
def dashboard_summary(db: Session = Depends(get_db), user: User = Depends(require_roles(*ALLOWED_ROLES))):
    total = db.scalar(select(func.count()).select_from(Application)) or 0
    pending = db.scalar(select(func.count()).select_from(Application).where(Application.status.in_(["Pending", "In Review", "Returned"]))) or 0
    overdue = sum(1 for a in db.scalars(select(Application)).all() if a.due_date and safe_dt(a.due_date) < utcnow() and a.status not in {"Completed", "Rejected"})
    stage_rows = db.execute(select(Application.current_stage, func.count()).group_by(Application.current_stage)).all()
    evc_upcoming = db.scalar(select(func.count()).select_from(EVCVisit).where(EVCVisit.status.in_(["Proposed", "Scheduled"]))) or 0
    compliance_open = db.scalar(select(func.count()).select_from(ComplianceItem).where(ComplianceItem.status != "Verified")) or 0
    alerts_open = db.scalar(select(func.count()).select_from(Alert).where(Alert.acknowledged.is_(False))) or 0
    return {"total_applications": total, "pending_applications": pending, "overdue_applications": overdue,
            "evc_upcoming": evc_upcoming, "compliance_open": compliance_open, "alerts_open": alerts_open,
            "stage_counts": [{"stage": stage, "count": count} for stage, count in stage_rows]}

@app.get("/api/v1/compliance")
def list_compliance(application_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(require_roles(*ALLOWED_ROLES))):
    stmt = select(ComplianceItem).order_by(ComplianceItem.due_date.asc())
    if application_id: stmt = stmt.where(ComplianceItem.application_id == application_id)
    rows = db.scalars(stmt).all()
    return [{"id": r.id, "application_id": r.application_id, "application_no": (db.get(Application, r.application_id).application_no if db.get(Application, r.application_id) else ""), "institution_name": (db.get(Application, r.application_id).institution_name if db.get(Application, r.application_id) else ""), "title": r.title, "category": r.category, "due_date": r.due_date, "status": r.status, "assigned_to": r.assigned_to, "notes": r.notes} for r in rows]

@app.post("/api/v1/compliance", status_code=201)
def create_compliance(payload: ComplianceCreate, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "officer"))):
    if not db.get(Application, payload.application_id): raise HTTPException(404, "Application not found")
    item = ComplianceItem(**payload.model_dump(), status="Pending")
    db.add(item); db.flush(); audit(db, "COMPLIANCE_CREATED", user.full_name, item.application_id, item.title); db.commit(); db.refresh(item)
    return {"id": item.id, "application_id": item.application_id, "title": item.title, "category": item.category, "due_date": item.due_date, "status": item.status, "assigned_to": item.assigned_to, "notes": item.notes}

@app.patch("/api/v1/compliance/{item_id}")
def update_compliance(item_id: int, payload: ComplianceUpdate, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "officer", "reviewer"))):
    item = db.get(ComplianceItem, item_id)
    if not item: raise HTTPException(404, "Compliance item not found")
    for key, value in payload.model_dump(exclude_unset=True).items(): setattr(item, key, value)
    item.updated_at = utcnow()
    audit(db, "COMPLIANCE_UPDATED", user.full_name, item.application_id, f"{item.title}: {item.status}")
    db.commit(); db.refresh(item)
    return {"id": item.id, "application_id": item.application_id, "title": item.title, "category": item.category, "due_date": item.due_date, "status": item.status, "assigned_to": item.assigned_to, "notes": item.notes}

@app.get("/api/v1/document-validity")
def list_document_validity(application_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(require_roles(*ALLOWED_ROLES))):
    stmt = select(DocumentValidity).order_by(DocumentValidity.expiry_date.asc())
    if application_id is not None:
        stmt = stmt.where(DocumentValidity.application_id == application_id)
    rows = db.scalars(stmt).all()
    return [{
        "id": x.id, "application_id": x.application_id, "application_no": db.get(Application, x.application_id).application_no if db.get(Application, x.application_id) else None,
        "document_id": x.document_id, "document_name": x.document_name, "category": x.category,
        "issued_on": x.issued_on, "expiry_date": x.expiry_date, "renewal_due_date": x.renewal_due_date,
        "status": x.status, "notes": x.notes, "created_by": x.created_by, "updated_by": x.updated_by,
        "created_at": x.created_at, "updated_at": x.updated_at
    } for x in rows]

@app.post("/api/v1/document-validity", status_code=201)
def create_document_validity(payload: DocumentValidityCreate, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "officer"))):
    application = db.get(Application, payload.application_id)
    if not application:
        raise HTTPException(404, "Application not found")
    if payload.document_id is not None:
        document = db.get(DocumentRecord, payload.document_id)
        if not document or document.application_id != application.id:
            raise HTTPException(422, "Linked document must belong to the selected application")
    if payload.issued_on and payload.expiry_date and safe_dt(payload.expiry_date) < safe_dt(payload.issued_on):
        raise HTTPException(422, "Expiry date cannot be earlier than issue date")
    item = DocumentValidity(**payload.model_dump(), status="Active", created_by=user.full_name, updated_by=user.full_name)
    db.add(item); db.flush()
    audit(db, "DOCUMENT_VALIDITY_CREATED", user.full_name, application.id, f"{item.document_name}; expiry={item.expiry_date}; renewal_due={item.renewal_due_date}")
    db.commit(); db.refresh(item)
    return {"id": item.id, "application_id": item.application_id, "document_name": item.document_name, "category": item.category, "document_id": item.document_id, "issued_on": item.issued_on, "expiry_date": item.expiry_date, "renewal_due_date": item.renewal_due_date, "status": item.status, "notes": item.notes, "created_by": item.created_by, "updated_by": item.updated_by, "created_at": item.created_at, "updated_at": item.updated_at}

@app.patch("/api/v1/document-validity/{item_id}")
def update_document_validity(item_id: int, payload: DocumentValidityUpdate, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "officer", "reviewer"))):
    item = db.get(DocumentValidity, item_id)
    if not item:
        raise HTTPException(404, "Document validity record not found")
    changes = payload.model_dump(exclude_unset=True)
    if "status" in changes and changes["status"] not in {"Active", "Renewal Due", "Under Verification", "Expired", "Renewed", "Not Applicable"}:
        raise HTTPException(422, "Unsupported document validity status")
    if "document_id" in changes and changes["document_id"] is not None:
        document = db.get(DocumentRecord, changes["document_id"])
        if not document or document.application_id != item.application_id:
            raise HTTPException(422, "Linked document must belong to the same application")
    issue = changes.get("issued_on", item.issued_on)
    expiry = changes.get("expiry_date", item.expiry_date)
    if issue and expiry and safe_dt(expiry) < safe_dt(issue):
        raise HTTPException(422, "Expiry date cannot be earlier than issue date")
    for key, value in changes.items():
        setattr(item, key, value)
    item.updated_by = user.full_name
    item.updated_at = utcnow()
    audit(db, "DOCUMENT_VALIDITY_UPDATED", user.full_name, item.application_id, f"{item.document_name}; changed={','.join(changes.keys())}; status={item.status}")
    db.commit(); db.refresh(item)
    return {"id": item.id, "application_id": item.application_id, "document_name": item.document_name, "category": item.category, "document_id": item.document_id, "issued_on": item.issued_on, "expiry_date": item.expiry_date, "renewal_due_date": item.renewal_due_date, "status": item.status, "notes": item.notes, "updated_by": item.updated_by, "updated_at": item.updated_at}

@app.get("/api/v1/undertakings")
def list_undertakings(application_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(require_roles(*ALLOWED_ROLES))):
    stmt = select(Undertaking).order_by(Undertaking.due_date.asc())
    if application_id: stmt = stmt.where(Undertaking.application_id == application_id)
    return [{"id": x.id, "application_id": x.application_id, "title": x.title, "submitted": x.submitted, "due_date": x.due_date, "notes": x.notes} for x in db.scalars(stmt).all()]

@app.post("/api/v1/undertakings", status_code=201)
def create_undertaking(payload: UndertakingCreate, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "officer"))):
    if not db.get(Application, payload.application_id): raise HTTPException(404, "Application not found")
    item = Undertaking(**payload.model_dump()); db.add(item); db.flush()
    audit(db, "UNDERTAKING_CREATED", user.full_name, item.application_id, item.title); db.commit(); db.refresh(item)
    return {"id": item.id, "application_id": item.application_id, "title": item.title, "submitted": item.submitted, "due_date": item.due_date, "notes": item.notes}

@app.patch("/api/v1/undertakings/{item_id}")
def update_undertaking(item_id: int, payload: UndertakingUpdate, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "officer"))):
    item = db.get(Undertaking, item_id)
    if not item: raise HTTPException(404, "Undertaking not found")
    for key, value in payload.model_dump(exclude_unset=True).items(): setattr(item, key, value)
    audit(db, "UNDERTAKING_UPDATED", user.full_name, item.application_id, item.title); db.commit(); db.refresh(item)
    return {"id": item.id, "application_id": item.application_id, "title": item.title, "submitted": item.submitted, "due_date": item.due_date, "notes": item.notes}

@app.get("/api/v1/evc")
def list_evc(db: Session = Depends(get_db), user: User = Depends(require_roles(*ALLOWED_ROLES))):
    rows = db.scalars(select(EVCVisit).order_by(EVCVisit.proposed_date.asc())).all()
    return [{"id": x.id, "application_id": x.application_id, "application_no": db.get(Application, x.application_id).application_no if db.get(Application, x.application_id) else "", "institution_name": db.get(Application, x.application_id).institution_name if db.get(Application, x.application_id) else "", "proposed_date": x.proposed_date, "location": x.location, "panel_members": x.panel_members, "status": x.status, "outcome": x.outcome} for x in rows]

@app.post("/api/v1/evc", status_code=201)
def create_evc(payload: EVCVisitCreate, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "officer"))):
    app_item = db.get(Application, payload.application_id)
    if not app_item: raise HTTPException(404, "Application not found")
    item = EVCVisit(**payload.model_dump(), status="Proposed"); db.add(item); db.flush()
    audit(db, "EVC_VISIT_CREATED", user.full_name, item.application_id, f"Visit proposed for {payload.proposed_date}")
    db.commit(); db.refresh(item)
    return {"id": item.id, "application_id": item.application_id, "proposed_date": item.proposed_date, "location": item.location, "panel_members": item.panel_members, "status": item.status, "outcome": item.outcome}

@app.patch("/api/v1/evc/{visit_id}")
def update_evc(visit_id: int, payload: EVCVisitUpdate, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "officer", "reviewer"))):
    item = db.get(EVCVisit, visit_id)
    if not item: raise HTTPException(404, "EVC visit not found")
    for key, value in payload.model_dump(exclude_unset=True).items(): setattr(item, key, value)
    audit(db, "EVC_VISIT_UPDATED", user.full_name, item.application_id, f"Status: {item.status}; outcome: {item.outcome or ''}")
    db.commit(); db.refresh(item)
    return {"id": item.id, "application_id": item.application_id, "proposed_date": item.proposed_date, "location": item.location, "panel_members": item.panel_members, "status": item.status, "outcome": item.outcome}

@app.get("/api/v1/alerts")
def list_alerts(db: Session = Depends(get_db), user: User = Depends(require_roles(*ALLOWED_ROLES))):
    sync_deadline_alerts(db)
    rows = db.scalars(select(Alert).order_by(Alert.created_at.desc())).all()
    return [{"id": a.id, "application_id": a.application_id, "application_no": db.get(Application, a.application_id).application_no if a.application_id and db.get(Application, a.application_id) else "", "title": a.title, "message": a.message, "severity": a.severity, "category": a.category, "acknowledged": a.acknowledged, "created_at": a.created_at, "acknowledged_by": a.acknowledged_by} for a in rows]

@app.patch("/api/v1/alerts/{alert_id}/acknowledge")
def acknowledge_alert(alert_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "officer", "reviewer"))):
    item = db.get(Alert, alert_id)
    if not item: raise HTTPException(404, "Alert not found")
    item.acknowledged = True; item.acknowledged_by = user.full_name
    audit(db, "ALERT_ACKNOWLEDGED", user.full_name, item.application_id, item.title)
    db.commit()
    return {"id": item.id, "acknowledged": True, "acknowledged_by": item.acknowledged_by}

@app.get("/api/v1/document-checklists/templates")
def list_document_checklist_templates(db: Session = Depends(get_db), user: User = Depends(require_roles(*ALLOWED_ROLES))):
    rows = db.scalars(select(DocumentChecklistTemplate).where(DocumentChecklistTemplate.active.is_(True)).order_by(DocumentChecklistTemplate.application_type, DocumentChecklistTemplate.id)).all()
    grouped = {}
    for row in rows:
        grouped.setdefault(row.application_type, []).append({"key": row.item_key, "label": row.label, "required": row.required})
    return {"templates": [{"application_type": key, "items": items} for key, items in sorted(grouped.items())],
            "note": "Templates are operator-configured examples. Verify required documents against current official instructions before use."}

@app.put("/api/v1/document-checklists/templates")
def save_document_checklist_template(payload: DocumentChecklistTemplateUpdate, db: Session = Depends(get_db), user: User = Depends(require_roles("admin"))):
    application_type = payload.application_type.strip()
    seen = set()
    cleaned = []
    for item in payload.items:
        key = str(item.get("key", "")).strip()
        label = str(item.get("label", "")).strip()
        required = item.get("required", True)
        if not key or len(key) > 80 or not label or len(label) > 240 or not isinstance(required, bool):
            raise HTTPException(422, "Each checklist item needs a key, label and boolean required flag")
        if key in seen:
            raise HTTPException(422, "Checklist item keys must be unique within an application type")
        seen.add(key)
        cleaned.append((key, label, required))
    db.query(DocumentChecklistTemplate).filter(DocumentChecklistTemplate.application_type == application_type).update({DocumentChecklistTemplate.active: False})
    for key, label, required in cleaned:
        db.add(DocumentChecklistTemplate(application_type=application_type, item_key=key, label=label, required=required, active=True, updated_by=user.full_name))
    audit(db, "DOCUMENT_CHECKLIST_TEMPLATE_UPDATED", user.full_name, None, f"{application_type}: {len(cleaned)} checklist item(s)")
    db.commit()
    return {"application_type": application_type, "items": [{"key": k, "label": label, "required": required} for k, label, required in cleaned]}

@app.post("/api/v1/applications/{application_id}/document-checklist/apply")
def apply_document_checklist(application_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "officer"))):
    application = db.get(Application, application_id)
    if not application:
        raise HTTPException(404, "Application not found")
    templates = db.scalars(select(DocumentChecklistTemplate).where(DocumentChecklistTemplate.application_type == application.application_type, DocumentChecklistTemplate.active.is_(True)).order_by(DocumentChecklistTemplate.id)).all()
    if not templates:
        raise HTTPException(422, "No checklist template configured for this application type. Ask an admin to configure one after verifying official requirements.")
    existing = {row.template_id for row in db.scalars(select(ApplicationDocumentCheck).where(ApplicationDocumentCheck.application_id == application_id)).all()}
    added = 0
    for template in templates:
        if template.id not in existing:
            db.add(ApplicationDocumentCheck(application_id=application_id, template_id=template.id, item_key=template.item_key, label=template.label, required=template.required, status="Missing", updated_by=user.full_name))
            added += 1
    audit(db, "DOCUMENT_CHECKLIST_APPLIED", user.full_name, application_id, f"Added {added} checklist item(s)")
    db.commit()
    return {"application_id": application_id, "items_added": added, "message": "Checklist applied. Verify uploaded documents against each item; upload presence alone does not establish authenticity or sufficiency."}

@app.get("/api/v1/applications/{application_id}/document-checklist")
def get_application_document_checklist(application_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles(*ALLOWED_ROLES))):
    application = db.get(Application, application_id)
    if not application:
        raise HTTPException(404, "Application not found")
    checks = db.scalars(select(ApplicationDocumentCheck).where(ApplicationDocumentCheck.application_id == application_id).order_by(ApplicationDocumentCheck.id)).all()
    required = [row for row in checks if row.required and row.status != "Not Applicable"]
    outstanding = [row for row in required if row.status not in {"Verified", "Not Applicable"}]
    return {"application_id": application_id, "application_type": application.application_type,
            "items": [{"id": row.id, "key": row.item_key, "label": row.label, "required": row.required, "status": row.status, "document_id": row.document_id, "notes": row.notes, "updated_by": row.updated_by, "updated_at": row.updated_at} for row in checks],
            "summary": {"total": len(checks), "required": len(required), "missing_required": len(outstanding), "verified": sum(row.status == "Verified" for row in checks), "ready_for_human_review": len(required) > 0 and not outstanding},
            "note": "Readiness is a checklist signal only, not a legal compliance finding or approval decision."}

@app.patch("/api/v1/document-checklist-items/{check_id}")
def update_document_check(check_id: int, payload: DocumentCheckUpdate, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "officer", "reviewer"))):
    check = db.get(ApplicationDocumentCheck, check_id)
    if not check:
        raise HTTPException(404, "Checklist item not found")
    if payload.status not in {"Missing", "Uploaded", "Verified", "Rejected", "Not Applicable"}:
        raise HTTPException(422, "Invalid checklist status")
    if payload.document_id is not None:
        document = db.get(DocumentRecord, payload.document_id)
        if not document or document.application_id != check.application_id:
            raise HTTPException(422, "Selected document must belong to the same application")
    resulting_document_id = payload.document_id if "document_id" in payload.model_fields_set else check.document_id
    if payload.status in {"Uploaded", "Verified"} and resulting_document_id is None:
        raise HTTPException(422, "Select an uploaded document before marking this item Uploaded or Verified")
    if payload.status == "Rejected" and not (payload.notes or check.notes or "").strip():
        raise HTTPException(422, "A reason is required when rejecting a checklist document")
    check.status = payload.status
    if "document_id" in payload.model_fields_set:
        check.document_id = payload.document_id
    if payload.notes is not None:
        check.notes = payload.notes
    check.updated_by = user.full_name
    check.updated_at = utcnow()
    audit(db, "DOCUMENT_CHECKLIST_ITEM_UPDATED", user.full_name, check.application_id, f"{check.label}: {check.status}")
    db.commit()
    return {"id": check.id, "status": check.status, "document_id": check.document_id, "updated_by": check.updated_by, "updated_at": check.updated_at}

@app.get("/api/v1/applications/{application_id}/documents")
def list_documents(application_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles(*ALLOWED_ROLES))):
    if not db.get(Application, application_id): raise HTTPException(404, "Application not found")
    rows = db.scalars(select(DocumentRecord).where(DocumentRecord.application_id == application_id).order_by(DocumentRecord.created_at.desc())).all()
    return [{"id": d.id, "application_id": d.application_id, "original_name": d.original_name, "content_type": d.content_type, "size_bytes": d.size_bytes, "uploaded_by": d.uploaded_by, "created_at": d.created_at} for d in rows]

@app.post("/api/v1/applications/{application_id}/documents", status_code=201)
async def upload_document(application_id: int, file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "officer"))):
    if not db.get(Application, application_id): raise HTTPException(404, "Application not found")
    safe_name = Path(file.filename or "document").name
    allowed = {".pdf", ".png", ".jpg", ".jpeg", ".docx", ".xlsx"}
    if Path(safe_name).suffix.lower() not in allowed: raise HTTPException(415, "Only PDF, PNG, JPG, DOCX and XLSX files are allowed")
    content = await file.read(settings.max_upload_mb * 1024 * 1024 + 1)
    if len(content) > settings.max_upload_mb * 1024 * 1024: raise HTTPException(413, f"File exceeds {settings.max_upload_mb} MB limit")
    storage_key = uuid4().hex + Path(safe_name).suffix.lower()
    target = Path(settings.upload_dir) / storage_key; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(content)
    record = DocumentRecord(application_id=application_id, original_name=safe_name, storage_key=storage_key,
        content_type=file.content_type or "application/octet-stream", size_bytes=len(content), uploaded_by=user.full_name)
    db.add(record); db.flush(); audit(db, "DOCUMENT_UPLOADED", user.full_name, application_id, safe_name); db.commit(); db.refresh(record)
    return {"id": record.id, "application_id": record.application_id, "original_name": record.original_name, "content_type": record.content_type, "size_bytes": record.size_bytes, "uploaded_by": record.uploaded_by, "created_at": record.created_at}

@app.get("/api/v1/documents/{document_id}/download")
def download_document(document_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles(*ALLOWED_ROLES))):
    record = db.get(DocumentRecord, document_id)
    if not record: raise HTTPException(404, "Document not found")
    path = Path(settings.upload_dir) / record.storage_key
    if not path.exists(): raise HTTPException(404, "Stored file is missing")
    return FileResponse(path, media_type=record.content_type, filename=record.original_name)

@app.get("/api/v1/reports/summary")
def report_summary(db: Session = Depends(get_db), user: User = Depends(require_roles(*ALLOWED_ROLES))):
    apps = db.scalars(select(Application)).all()
    stages = {}
    for item in apps: stages[item.current_stage] = stages.get(item.current_stage, 0) + 1
    states = {}
    for item in apps: states[item.state] = states.get(item.state, 0) + 1
    return {"generated_at": utcnow(), "total": len(apps), "completed": sum(a.status == "Completed" for a in apps),
            "open": sum(a.status != "Completed" and a.status != "Rejected" for a in apps),
            "overdue": sum(bool(a.due_date and safe_dt(a.due_date) < utcnow() and a.status != "Completed") for a in apps),
            "by_stage": [{"stage": k, "count": v} for k,v in sorted(stages.items())],
            "by_state": [{"state": k, "count": v} for k,v in sorted(states.items())]}

@app.get("/api/v1/reports/export.csv")
def export_csv(db: Session = Depends(get_db), user: User = Depends(require_roles(*ALLOWED_ROLES))):
    output = io.StringIO(); writer = csv.writer(output)
    writer.writerow(["Application No", "Institution", "State", "Type", "Stage", "Status", "Priority", "Assignee", "Submitted At", "Due Date"])
    for a in db.scalars(select(Application).order_by(Application.submitted_at.desc())).all():
        writer.writerow([a.application_no, a.institution_name, a.state, a.application_type, a.current_stage, a.status, a.priority, a.assigned_to or "", a.submitted_at.isoformat() if a.submitted_at else "", a.due_date.isoformat() if a.due_date else ""])
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=aicte-applications-report.csv"})

@app.get("/api/v1/applications/{application_id}/risk")
def application_risk(application_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles(*ALLOWED_ROLES))):
    item = db.get(Application, application_id)
    if not item: raise HTTPException(404, "Application not found")
    score, factors = 0, []
    if item.priority == "High": score += 30; factors.append("High priority adds 30 points")
    elif item.priority == "Normal": score += 10; factors.append("Normal priority adds 10 points")
    if item.due_date:
        days = (safe_dt(item.due_date) - utcnow()).total_seconds()/86400
        if days < 0: score += 45; factors.append("Deadline overdue adds 45 points")
        elif days <= 3: score += 30; factors.append("Deadline within 3 days adds 30 points")
        elif days <= 7: score += 15; factors.append("Deadline within 7 days adds 15 points")
    open_compliance = db.scalar(select(func.count()).select_from(ComplianceItem).where(ComplianceItem.application_id == item.id, ComplianceItem.status != "Verified")) or 0
    if open_compliance: score += min(25, open_compliance*10); factors.append(f"{open_compliance} compliance item(s) not verified")
    score = min(100, score)
    return {"application_id": item.id, "application_no": item.application_no, "score": score,
            "band": "High" if score >= 60 else ("Medium" if score >= 30 else "Low"),
            "factors": factors, "disclaimer": "Explainable triage aid only; not an AICTE decision or statutory finding."}

@app.get("/api/v1/audit")
def list_audit(limit: int = 100, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "auditor"))):
    rows = db.scalars(select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(min(max(limit,1),500))).all()
    return [{"id": x.id, "application_id": x.application_id, "action": x.action, "actor": x.actor, "details": x.details, "created_at": x.created_at} for x in rows]
