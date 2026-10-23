import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
import csv, io, json, threading, random
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.config import settings
from app.database import Base, engine, get_db, SessionLocal
from app.models import (Alert, Application, AuditEvent, ComplianceItem, DocumentRecord,
                        EVCVisit, Undertaking, User, WorkflowEvent, WorkflowConfiguration, ApplicationWorkflowRoute, DocumentChecklistTemplate, ApplicationDocumentCheck, DocumentValidity, DocumentQualityCheck, StageChecklistItem, CollegeProfile, ComplianceStatusHistory)
from app.schemas import (ApplicationCreate, ApplicationOut, ApplicationUpdate, ComplianceCreate,
                         ComplianceUpdate, EVCVisitCreate, EVCVisitUpdate, LoginIn,
                         TransitionIn, UndertakingCreate, UndertakingUpdate, WorkflowConfigUpdate, ApplicationWorkflowRouteUpdate, DocumentChecklistTemplateUpdate, DocumentCheckUpdate, DocumentValidityCreate, DocumentValidityUpdate)
from app.security import create_access_token, hash_password, require_roles, verify_password
from app.intake import router as intake_router

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



def seed_demo_documents(db: Session, application: Application, uploaded_by: str) -> None:
    """Create four clearly-labelled synthetic PDF samples without extra dependencies."""
    slots = [
        ("Review EVC report and findings", "DEMO-EVC-001"),
        ("Review facilities, faculty and infrastructure observations", "DEMO-FAC-002"),
        ("Record committee recommendation and rationale", "DEMO-REC-003"),
        ("Confirm any conditions or compliance items required", "DEMO-COMP-004"),
    ]
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    existing_slots = set(db.scalars(select(DocumentQualityCheck.slot_name).where(DocumentQualityCheck.application_id == application.id)).all())
    for slot, reference in slots:
        if slot in existing_slots:
            continue
        lines = ["SYNTHETIC DEMO DOCUMENT", slot, f"Institution: {application.institution_name}",
                 f"Application: {application.application_no}", f"Reference: {reference}",
                 "NOT an official AICTE record. For prototype review only."]
        def esc(value: str) -> str:
            return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")[:110]
        commands = ["BT", "/F1 12 Tf", "50 790 Td"]
        for index, line in enumerate(lines):
            if index:
                commands.append("0 -28 Td")
            commands.append(f"({esc(line)}) Tj")
        commands.append("ET")
        stream = "\n".join(commands).encode("latin-1", "replace")
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        ]
        payload = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        for number, obj in enumerate(objects, 1):
            offsets.append(len(payload))
            payload.extend(f"{number} 0 obj\n".encode()); payload.extend(obj); payload.extend(b"\nendobj\n")
        xref = len(payload)
        payload.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
        for offset in offsets[1:]:
            payload.extend(f"{offset:010d} 00000 n \n".encode())
        payload.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
        storage_key = uuid4().hex + ".pdf"
        (upload_dir / storage_key).write_bytes(bytes(payload))
        record = DocumentRecord(application_id=application.id, original_name=f"DEMO - {slot}.pdf",
            storage_key=storage_key, content_type="application/pdf", size_bytes=len(payload), uploaded_by=uploaded_by)
        db.add(record); db.flush()
        db.add(DocumentQualityCheck(document_id=record.id, application_id=application.id, slot_name=slot,
            status="Needs Review", findings_json=json.dumps(["Synthetic demo sample; open and review before verifying the related checklist item."])))

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
    for item in db.scalars(select(ComplianceItem)).all():
        if not item.due_date:
            continue
        days = (safe_dt(item.due_date) - now).total_seconds() / 86400
        # Completed decisions are never downgraded to overdue by the scheduler.
        if days < 0 and item.status not in {"Compliant", "Non-Compliant", "Verified", "Submitted", "Overdue"}:
            previous_status = item.status
            item.status = "Overdue"
            item.updated_at = now
            db.add(ComplianceStatusHistory(compliance_item_id=item.id, from_status=previous_status, to_status="Overdue",
                note="Scheduled deadline check marked unresolved task overdue.", changed_by="system"))
        if days <= 3 and item.status not in {"Compliant", "Non-Compliant", "Verified", "Submitted"}:
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
ALLOWED_ROLES = {"admin", "officer", "reviewer", "auditor", "scrutiny_officer", "evc_officer", "sac_shc_officer", "compliance_officer", "final_approval_officer"}

def utcnow():
    return datetime.now(timezone.utc)

def safe_dt(value):
    if value is None: return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

def audit(db: Session, action: str, actor: str, application_id: int | None = None, details: str | None = None):
    db.add(AuditEvent(application_id=application_id, action=action, actor=actor, details=details))

def ensure_stage_checklist(db: Session, application: Application, stage: str):
    """Create actionable review items when an application first reaches a workflow stage."""
    templates = {
        "SAC/SHC Review": [
            ("Review EVC report and findings", True),
            ("Review facilities, faculty and infrastructure observations", True),
            ("Record committee recommendation and rationale", True),
            ("Confirm any conditions or compliance items required", True),
        ],
        "Compliance": [
            ("Record each committee condition requiring compliance", True),
            ("Verify evidence submitted against each condition", True),
            ("Record compliance recommendation for authorised review", True),
        ],
    }
    existing = {row.title for row in db.scalars(select(StageChecklistItem).where(
        StageChecklistItem.application_id == application.id,
        StageChecklistItem.stage == stage,
    )).all()}
    for title, required in templates.get(stage, []):
        if title not in existing:
            db.add(StageChecklistItem(application_id=application.id, stage=stage, title=title,
                                      required=required, status="Pending",
                                      notes="Default workflow task; verify against applicable official requirements.",
                                      updated_by="system"))


SYNTHETIC_APPLICATION_TARGET = 900
SYNTHETIC_PREFIX = "SYN-2026-"

def seed_synthetic_applications(db: Session) -> int:
    """Idempotently create a realistic, explicitly synthetic local demo portfolio."""
    if settings.app_env.strip().lower() in {"prod", "production"}:
        return 0
    if not db.scalar(select(User.id).where(User.username == "admin@demo.local")):
        return 0
    existing_numbers = set(db.scalars(select(Application.application_no).where(
        Application.application_no.like(f"{SYNTHETIC_PREFIX}%"))).all())
    if len(existing_numbers) >= SYNTHETIC_APPLICATION_TARGET:
        return 0
    rng = random.Random(20261024)
    institutions = [
        "North Valley Institute of Technology", "Saraswati College of Engineering",
        "Riverbend Institute of Management", "Pioneer Polytechnic Institute",
        "Aryavart School of Engineering", "National Institute of Applied Sciences",
        "Shivneri Technical Campus", "Eastern Gateway College of Technology",
        "Greenfield Institute of Computing", "Pragati College of Engineering",
        "Horizon Institute of Professional Studies", "Central Institute of Design and Technology",
        "Lotus Valley School of Management", "Vindhya Institute of Technology",
        "Sapphire College of Applied Engineering", "Unity Institute of Technical Education",
        "Crescent School of Engineering", "Deccan Institute of Digital Technology",
        "Blue Ridge Polytechnic", "Sunrise Institute of Science and Management",
    ]
    states = [
        ("Uttar Pradesh", ["Noida", "Lucknow", "Kanpur", "Agra", "Varanasi", "Ghaziabad"]),
        ("Maharashtra", ["Pune", "Nagpur", "Nashik", "Aurangabad", "Mumbai"]),
        ("Rajasthan", ["Jaipur", "Jodhpur", "Kota", "Udaipur"]),
        ("Madhya Pradesh", ["Bhopal", "Indore", "Jabalpur", "Gwalior"]),
        ("Karnataka", ["Bengaluru", "Mysuru", "Hubballi", "Mangaluru"]),
        ("Tamil Nadu", ["Chennai", "Coimbatore", "Madurai", "Salem"]),
        ("Gujarat", ["Ahmedabad", "Surat", "Vadodara", "Rajkot"]),
        ("Telangana", ["Hyderabad", "Warangal", "Karimnagar"]),
        ("Haryana", ["Gurugram", "Faridabad", "Rohtak"]),
        ("Uttarakhand", ["Dehradun", "Haridwar", "Roorkee"]),
        ("Bihar", ["Patna", "Gaya", "Muzaffarpur"]),
        ("West Bengal", ["Kolkata", "Durgapur", "Siliguri"]),
    ]
    types = ["Technical Institution", "Engineering College", "Polytechnic", "Management Institution"]
    application_types = ["New Institution", "Extension of Approval", "Increase in Intake", "New Course"]
    stages = ["Application Intake", "Scrutiny", "Re-scrutiny", "EVC Scheduling", "EVC Visit",
              "SAC/SHC Review", "Compliance", "Approved / Closed", "Returned to Institution", "Rejected"]
    weights = [7, 22, 10, 9, 7, 12, 15, 8, 6, 4]
    owner = {"Application Intake":"Case Officer","Scrutiny":"Scrutiny Officer","Re-scrutiny":"Scrutiny Officer",
             "EVC Scheduling":"EVC Officer","EVC Visit":"EVC Officer","SAC/SHC Review":"SAC/SHC Officer",
             "Compliance":"Compliance Officer","Approved / Closed":"Final Approval Officer",
             "Returned to Institution":"Scrutiny Officer","Rejected":"Final Approval Officer"}
    statuses = {"Application Intake":["Pending","In Review"],"Scrutiny":["In Review","Pending"],
                 "Re-scrutiny":["In Review","Returned"],"EVC Scheduling":["In Review","Pending"],
                 "EVC Visit":["In Review","Pending"],"SAC/SHC Review":["In Review","Pending"],
                 "Compliance":["In Review","Pending"],"Approved / Closed":["Completed"],
                 "Returned to Institution":["Returned"],"Rejected":["Rejected"]}
    created = 0
    for number in range(1, SYNTHETIC_APPLICATION_TARGET + 1):
        application_no = f"{SYNTHETIC_PREFIX}{number:04d}"
        if application_no in existing_numbers:
            continue
        state, cities = rng.choice(states)
        city = rng.choice(cities)
        stage = rng.choices(stages, weights=weights, k=1)[0]
        submitted = utcnow() - timedelta(days=rng.randint(0, 330), hours=rng.randint(0, 23))
        due = submitted + timedelta(days=rng.choice([30, 45, 60, 90, 120, 180]))
        status = "Completed" if stage == "Approved / Closed" else ("Rejected" if stage == "Rejected" else ("Returned" if stage == "Returned to Institution" else rng.choice(statuses[stage])))
        app = Application(application_no=application_no,
            institution_name=f"{rng.choice(institutions)} - {city} {number:03d}",
            institution_type=rng.choice(types), state=state, application_type=rng.choice(application_types),
            current_stage=stage, status=status,
            priority=rng.choices(["Low","Normal","High","Critical"], weights=[15,55,24,6], k=1)[0],
            assigned_to=owner[stage], submitted_at=submitted, due_date=due,
            notes=f"SYNTHETIC DEMO RECORD. City: {city}. Not an actual institution or official AICTE application.")
        db.add(app); db.flush()
        db.add(WorkflowEvent(application_id=app.id, from_stage=None, to_stage="Application Intake",
            status="Submitted", note="Synthetic demo application created for workflow testing.",
            actor="Synthetic demo generator", created_at=submitted))
        if stage != "Application Intake":
            db.add(WorkflowEvent(application_id=app.id, from_stage="Application Intake", to_stage=stage,
                status=status, note="Synthetic stage placement for dashboard testing; not an official decision.",
                actor="Synthetic demo generator", created_at=min(utcnow(), submitted + timedelta(days=rng.randint(1,25)))))
        created += 1
    db.flush()
    return created

def seed_synthetic_supporting_records(db: Session) -> None:
    """Attach synthetic checklists, validity metadata and compliance work to synthetic cases."""
    if settings.app_env.strip().lower() in {"prod", "production"}:
        return
    apps = db.scalars(select(Application).where(Application.application_no.like(f"{SYNTHETIC_PREFIX}%"))).all()
    rng = random.Random(20261025)
    templates = [("registration","Institution registration / approval proof"),
                 ("land_building","Land / building evidence"),
                 ("faculty_infrastructure","Faculty / infrastructure details")]
    order = {name: idx for idx, name in enumerate(WORKFLOW_STAGES)}
    for app in apps:
        docs = db.scalars(select(DocumentRecord).where(DocumentRecord.application_id == app.id).order_by(DocumentRecord.id)).all()
        existing_checks = db.scalars(select(ApplicationDocumentCheck).where(ApplicationDocumentCheck.application_id == app.id)).all()
        by_key = {row.item_key: row for row in existing_checks}
        stage_index = order.get(app.current_stage, 1)
        for idx, (key, label) in enumerate(templates):
            doc = docs[idx] if idx < len(docs) else None
            if key in by_key:
                if by_key[key].document_id is None and doc is not None and by_key[key].status != "Missing":
                    by_key[key].document_id = doc.id
                continue
            if doc is None:
                status = "Missing"
            elif stage_index >= order["EVC Scheduling"]:
                status = "Verified"
            elif stage_index <= order["Scrutiny"]:
                status = rng.choices(["Pending","Verified","Needs Review","Missing"], [42,35,15,8], k=1)[0]
            else:
                status = rng.choices(["Verified","Pending","Needs Review"], [72,18,10], k=1)[0]
            db.add(ApplicationDocumentCheck(application_id=app.id, item_key=key, label=label,
                required=True, status=status, document_id=doc.id if doc and status != "Missing" else None,
                notes="Synthetic demo checklist item; validate against current official guidance.",
                updated_by="Synthetic demo generator"))
        if not db.scalar(select(DocumentValidity.id).where(DocumentValidity.application_id == app.id)):
            offset = rng.choice([-60,-10,5,20,60,120,365])
            expiry = utcnow() + timedelta(days=offset)
            db.add(DocumentValidity(application_id=app.id, document_id=docs[0].id if docs else None,
                document_name="Synthetic building / occupancy certificate", category="Illustrative validity metadata",
                issued_on=expiry-timedelta(days=365), expiry_date=expiry, renewal_due_date=expiry-timedelta(days=30),
                status="Expired" if offset < 0 else ("Expiring" if offset <= 30 else "Active"),
                notes="Synthetic sample date for automation demonstration only; not an AICTE-mandated validity period.",
                created_by="Synthetic demo generator", updated_by="Synthetic demo generator"))
        if app.current_stage in {"Compliance","Approved / Closed"} and not db.scalar(select(Undertaking.id).where(Undertaking.application_id == app.id)):
            db.add(Undertaking(application_id=app.id, title="Synthetic compliance undertaking",
                submitted=rng.random()<0.68, due_date=app.due_date,
                notes="Illustrative demo undertaking only; replace with configured official requirement."))
        if app.current_stage == "Compliance" and not db.scalar(select(ComplianceItem.id).where(ComplianceItem.application_id == app.id)):
            for title, category in [("Verify recorded undertaking status","Undertaking"),
                                    ("Review illustrative certificate validity","Document Validity")]:
                due = app.due_date
                # SQLite may return naive datetimes while utcnow() is timezone-aware.
                # Normalize naive due dates to UTC before comparing them.
                due_utc = due.replace(tzinfo=timezone.utc) if due and due.tzinfo is None else due
                status = "Compliant" if rng.random()<0.45 else ("Overdue" if due_utc and due_utc<utcnow() else "Pending")
                db.add(ComplianceItem(application_id=app.id, title=title, category=category,
                    due_date=due, status=status, assigned_to=app.assigned_to,
                    notes="Synthetic demonstration task; not a statutory determination."))
        if app.current_stage in {"SAC/SHC Review","Compliance","Approved / Closed"} and not db.scalar(select(StageChecklistItem.id).where(
            StageChecklistItem.application_id == app.id, StageChecklistItem.stage == "SAC/SHC Review")):
            db.add(StageChecklistItem(application_id=app.id, stage="SAC/SHC Review",
                title="Review synthetic committee recommendation", required=True,
                status="Verified" if app.current_stage in {"Compliance","Approved / Closed"} else "Pending",
                notes="Synthetic demo checklist item; confirm actual committee requirements before use.",
                updated_by="Synthetic demo generator"))
        if app.current_stage in {"Compliance","Approved / Closed"} and not db.scalar(select(StageChecklistItem.id).where(
            StageChecklistItem.application_id == app.id, StageChecklistItem.stage == "Compliance")):
            db.add(StageChecklistItem(application_id=app.id, stage="Compliance",
                title="Review synthetic compliance undertaking", required=True,
                status="Verified" if app.current_stage == "Approved / Closed" else "Pending",
                notes="Synthetic demo checklist item; confirm actual conditions and deadlines from official records.",
                updated_by="Synthetic demo generator"))
    db.flush()

def seed_demo_data(db: Session):
    """Seed local demo accounts only; never create synthetic application records."""
    if settings.app_env.strip().lower() in {"prod", "production"}:
        return
    existing_users = {row.username for row in db.scalars(select(User)).all()}
    # Upgrade an existing local demo database with the new stage-role accounts, but
    # do not seed credentials into a database that does not use the known demo admin.
    if existing_users and "admin@demo.local" not in existing_users:
        return
    demo_users = [
        ("admin@demo.local", "Demo Administrator", "admin"),
        ("officer@demo.local", "Case Officer", "officer"),
        ("reviewer@demo.local", "Scrutiny Reviewer", "reviewer"),
        ("auditor@demo.local", "Audit Viewer", "auditor"),
        ("scrutiny@demo.local", "Scrutiny Officer", "scrutiny_officer"),
        ("evc@demo.local", "EVC Officer", "evc_officer"),
        ("sac.shc@demo.local", "SAC/SHC Officer", "sac_shc_officer"),
        ("compliance@demo.local", "Compliance Officer", "compliance_officer"),
        ("final.approval@demo.local", "Final Approval Officer", "final_approval_officer"),
    ]
    for username, full_name, role in demo_users:
        if username not in existing_users:
            db.add(User(username=username, full_name=full_name, role=role, password_hash=hash_password("Demo@12345")))
    db.commit()

@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_demo_data(db)
        # Seed 900 deterministic synthetic applications in non-production environments.
        seed_synthetic_applications(db)
        # Backfill clearly labelled synthetic PDF samples for the application register.
        for existing_application in db.scalars(select(Application)).all():
            seed_demo_documents(db, existing_application, "Synthetic demo generator")
        seed_synthetic_supporting_records(db)
        db.commit()
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
cors_origins = settings.cors_origin_list
if settings.app_env.strip().lower() not in {"prod", "production"}:
    cors_origins = list(dict.fromkeys([*cors_origins, "http://localhost:5173", "http://127.0.0.1:5173"]))
app.add_middleware(CORSMiddleware, allow_origins=cors_origins, allow_credentials=True, allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], allow_headers=["Authorization", "Content-Type"])

app.include_router(intake_router)

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
def me(user: User = Depends(require_roles(*ALLOWED_ROLES, "college"))):
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


_compliance_automation_lock = threading.Lock()

def _automation_task(db: Session, application: Application, title: str, category: str,
                     due_date=None, notes: str | None = None, assigned_to: str | None = None):
    existing = db.scalar(select(ComplianceItem).where(
        ComplianceItem.application_id == application.id, ComplianceItem.title == title))
    if existing:
        changed = False
        # Preserve officer verification decisions; only refresh unresolved deadline status.
        if existing.status not in {"Compliant", "Non-Compliant"} and due_date and existing.due_date != due_date:
            existing.due_date = due_date
            changed = True
        if existing.status not in {"Compliant", "Non-Compliant"} and existing.due_date and safe_dt(existing.due_date) < utcnow() and existing.status != "Overdue":
            previous = existing.status
            existing.status = "Overdue"
            existing.updated_at = utcnow()
            db.add(ComplianceStatusHistory(compliance_item_id=existing.id, from_status=previous, to_status="Overdue",
                note="Automatically marked overdue; source deadline has passed.", changed_by="system"))
            changed = True
        if changed:
            existing.updated_at = utcnow()
        return ("updated" if changed else "skipped"), existing
    task = ComplianceItem(application_id=application.id, title=title, category=category,
        due_date=due_date, status="Overdue" if due_date and safe_dt(due_date) < utcnow() else "Pending",
        assigned_to=assigned_to or application.assigned_to,
        notes=notes)
    db.add(task)
    db.flush()
    db.add(ComplianceStatusHistory(compliance_item_id=task.id, from_status=None, to_status=task.status,
        note="Generated by compliance automation from an existing application record.", changed_by="system"))
    return "created", task

@app.post("/api/v1/compliance/automation/run")
def run_compliance_automation(db: Session = Depends(get_db),
                              user: User = Depends(require_roles("admin", "officer", "compliance_officer"))):
    if not _compliance_automation_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Compliance automation is already running. Wait for the current run to finish.")
    run_id = str(uuid4())
    started = utcnow()
    stats = {"run_id": run_id, "status": "Running", "started_at": started.isoformat(),
        "completed_at": None, "applications_scanned": 0, "documents_checked": 0,
        "issues_detected": 0, "tasks_created": 0, "tasks_updated": 0,
        "tasks_skipped": 0, "alerts_created": 0, "errors": [], "warnings": [], "issues": []}
    try:
        # Scope limited to records visible to the role. Compliance officers only see their workflow stage.
        app_stmt = select(Application).order_by(Application.submitted_at.desc())
        if user.role == "compliance_officer":
            app_stmt = app_stmt.where(Application.current_stage == "Compliance")
        applications = db.scalars(app_stmt).all()
        stats["applications_scanned"] = len(applications)
        app_ids = {row.id for row in applications}
        if not app_ids:
            stats["status"] = "Completed"
            stats["completed_at"] = utcnow().isoformat()
            audit(db, "COMPLIANCE_AUTOMATION_RUN", user.full_name, None, json.dumps(stats))
            db.commit()
            return stats

        documents = db.scalars(select(DocumentRecord).where(DocumentRecord.application_id.in_(app_ids))).all()
        stats["documents_checked"] = len(documents)
        documents_by_app = {}
        for doc in documents:
            documents_by_app.setdefault(doc.application_id, []).append(doc)

        def issue(app, title, category, detail, due=None, source_id=None):
            stats["issues_detected"] += 1
            entry = {"application_id": app.id, "application_no": app.application_no,
                "institution_name": app.institution_name, "requirement": title,
                "category": category, "detail": detail, "source_id": source_id}
            stats["issues"].append(entry)
            outcome, task = _automation_task(db, app, title, category, due, detail)
            stats["tasks_created" if outcome == "created" else "tasks_updated" if outcome == "updated" else "tasks_skipped"] += 1
            if outcome == "created":
                db.add(Alert(application_id=app.id, title="Compliance task generated",
                    message=f"{app.application_no}: {title}. {detail}", severity="Medium", category="Compliance"))
                stats["alerts_created"] += 1

        for app in applications:
            app_docs = documents_by_app.get(app.id, [])
            doc_ids = {doc.id for doc in app_docs}
            required_checks = db.scalars(select(ApplicationDocumentCheck).where(
                ApplicationDocumentCheck.application_id == app.id,
                ApplicationDocumentCheck.required.is_(True))).all()
            for check in required_checks:
                if check.status not in {"Verified", "Not Applicable"} and (check.status in {"Missing", "Pending", "Needs Review", "Rejected"} or not check.document_id):
                    issue(app, f"Missing or unverified document: {check.label}", "Other",
                        f"Required checklist item status is '{check.status}'. Human review is required; this is not a statutory decision.", source_id=check.id)

            quality_rows = db.scalars(select(DocumentQualityCheck).where(DocumentQualityCheck.application_id == app.id)).all()
            for quality in quality_rows:
                if quality.document_id not in doc_ids:
                    continue
                doc = next((d for d in app_docs if d.id == quality.document_id), None)
                if not doc:
                    continue
                try:
                    findings = json.loads(quality.findings_json or "[]")
                except (ValueError, TypeError):
                    findings = []
                text_findings = " ".join(str(x) for x in findings)
                status_text = (quality.status + " " + text_findings).lower()
                if any(term in status_text for term in ("blur", "unreadable", "illegible", "corrupt", "cannot open")) and quality.status not in {"Verified", "Compliant"}:
                    issue(app, f"Document requires readability review: {doc.original_name}", "Other",
                        f"Existing screening record indicates a possible readability issue: {text_findings or quality.status}. Verify manually; not treated as expired.", source_id=doc.id)

            validity_rows = db.scalars(select(DocumentValidity).where(DocumentValidity.application_id == app.id)).all()
            for validity in validity_rows:
                due = validity.renewal_due_date or validity.expiry_date
                expired = bool(validity.expiry_date and safe_dt(validity.expiry_date) < utcnow())
                renewal_due = bool(validity.renewal_due_date and safe_dt(validity.renewal_due_date) < utcnow())
                expiring_soon = bool(validity.expiry_date and utcnow() <= safe_dt(validity.expiry_date) <= utcnow() + timedelta(days=30))
                if expired or renewal_due or expiring_soon or validity.status.lower() in {"expired", "expiring", "renewal due"}:
                    label = "Expired document" if expired or validity.status.lower() == "expired" else ("Document expiring within 30 days" if expiring_soon else "Document renewal due")
                    issue(app, f"{label}: {validity.document_name}", "Document Validity",
                        f"Recorded validity metadata: status={validity.status}; expiry={validity.expiry_date}; renewal due={validity.renewal_due_date}. Confirm source record before taking action.",
                        due=due, source_id=validity.id)

            # Only turn explicit, non-template committee-condition notes into tasks.
            condition_rows = db.scalars(select(StageChecklistItem).where(
                StageChecklistItem.application_id == app.id,
                StageChecklistItem.stage == "Compliance",
                StageChecklistItem.required.is_(True),
                StageChecklistItem.status.not_in(["Verified", "Not Applicable"]))).all()
            for condition in condition_rows:
                note = (condition.notes or "").strip()
                if not note or note.lower().startswith("default workflow task"):
                    continue
                issue(app, f"Approval condition: {condition.title}", "Approval Condition",
                    f"Recorded committee condition / officer note: {note}. No deadline is assigned because the source record has no due date.",
                    source_id=condition.id)

            for undertaking in db.scalars(select(Undertaking).where(Undertaking.application_id == app.id, Undertaking.submitted.is_(False))).all():
                issue(app, f"Pending undertaking: {undertaking.title}", "Undertaking",
                    f"Undertaking has not been marked submitted. {undertaking.notes or 'Manual follow-up required.'}",
                    due=undertaking.due_date, source_id=undertaking.id)

            for task in db.scalars(select(ComplianceItem).where(ComplianceItem.application_id == app.id)).all():
                if task.status not in {"Compliant", "Non-Compliant", "Overdue"} and task.due_date and safe_dt(task.due_date) < utcnow():
                    prior = task.status
                    task.status = "Overdue"
                    task.updated_at = utcnow()
                    db.add(ComplianceStatusHistory(compliance_item_id=task.id, from_status=prior, to_status="Overdue",
                        note="Automatically marked overdue because the recorded due date passed.", changed_by="system"))
                    stats["tasks_updated"] += 1
                    stats["issues_detected"] += 1
                    stats["issues"].append({"application_id": app.id, "application_no": app.application_no,
                        "institution_name": app.institution_name, "requirement": task.title,
                        "category": task.category, "detail": "Recorded task deadline has passed; unresolved task marked Overdue.",
                        "source_id": task.id})

        stats["status"] = "Completed"
        stats["completed_at"] = utcnow().isoformat()
        audit(db, "COMPLIANCE_AUTOMATION_RUN", user.full_name, None, json.dumps(stats, default=str))
        db.commit()
        return stats
    except Exception as exc:
        db.rollback()
        stats["status"] = "Failed"
        stats["completed_at"] = utcnow().isoformat()
        stats["errors"].append(str(exc)[:500])
        try:
            audit(db, "COMPLIANCE_AUTOMATION_RUN", user.full_name, None, json.dumps(stats, default=str))
            db.commit()
        except Exception:
            db.rollback()
        raise HTTPException(status_code=500, detail=f"Compliance automation failed (run {run_id}). No partial task changes were committed.")
    finally:
        _compliance_automation_lock.release()

@app.get("/api/v1/compliance/automation/last-run")
def get_last_compliance_automation(db: Session = Depends(get_db),
                                   user: User = Depends(require_roles("admin", "officer", "compliance_officer"))):
    row = db.scalar(select(AuditEvent).where(AuditEvent.action == "COMPLIANCE_AUTOMATION_RUN").order_by(AuditEvent.created_at.desc()))
    if not row or not row.details:
        return None
    # Do not expose an administrator's cross-application run details to a stage-scoped officer.
    if user.role == "compliance_officer" and row.actor != user.full_name:
        return None
    try:
        return json.loads(row.details)
    except (ValueError, TypeError):
        return {"status": "Unknown", "completed_at": row.created_at.isoformat(), "errors": ["Stored run details could not be parsed."]}

@app.post("/api/v1/automation/run")
def run_workflow_automation(db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "officer"))):
    # Safe operational automation: assign unowned cases, flag missing intake evidence,
    # and refresh deadline alerts. It never approves/rejects or advances statutory stages.
    open_apps = db.scalars(select(Application).where(Application.status.not_in(["Completed", "Rejected"]))).all()
    assigned = 0
    alerts_created = 0
    checks = []
    stage_role_map = {
        "Application Intake": "scrutiny_officer",
        "Scrutiny": "scrutiny_officer",
        "Re-scrutiny": "scrutiny_officer",
        "Returned to Institution": "scrutiny_officer",
        "EVC Scheduling": "evc_officer",
        "EVC Visit": "evc_officer",
        "SAC/SHC Review": "sac_shc_officer",
        "Compliance": "compliance_officer",
        "Final Approval": "final_approval_officer",
    }
    for item in open_apps:
        if not (item.assigned_to or "").strip():
            target_role = stage_role_map.get(item.current_stage)
            target_user = db.scalar(
                select(User).where(User.role == target_role, User.active.is_(True)).order_by(User.id)
            ) if target_role else None
            if target_user:
                item.assigned_to = target_user.full_name
                assigned += 1
                audit(
                    db, "AUTOMATION_CASE_ASSIGNED", user.full_name, item.id,
                    f"Routed case to active stage owner {target_user.username} for stage {item.current_stage}.",
                )
            else:
                title = "Workflow assignment requires configuration"
                exists = db.scalar(select(Alert.id).where(
                    Alert.application_id == item.id,
                    Alert.category == "Automation",
                    Alert.title == title,
                    Alert.acknowledged.is_(False),
                ))
                if not exists:
                    db.add(Alert(
                        application_id=item.id, title=title,
                        message=f"{item.application_no}: no active authorised user is configured for stage '{item.current_stage}'. An administrator must configure the stage owner; the case was left unassigned.",
                        severity="High", category="Automation",
                    ))
                    alerts_created += 1
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
    role_stages = {
        "scrutiny_officer": {"Application Intake", "Scrutiny", "Re-scrutiny", "Returned to Institution"},
        "evc_officer": {"EVC Scheduling", "EVC Visit"},
        "sac_shc_officer": {"SAC/SHC Review"},
        "compliance_officer": {"Compliance"},
        "final_approval_officer": {"Compliance"},
    }
    if user.role in role_stages:
        stmt = stmt.where(Application.current_stage.in_(role_stages[user.role]))
    if q:
        stmt = stmt.where(Application.institution_name.ilike(f"%{q}%") | Application.application_no.ilike(f"%{q}%") | Application.state.ilike(f"%{q}%"))
    if status and status != "All": stmt = stmt.where(Application.status == status)
    if stage and stage != "All": stmt = stmt.where(Application.current_stage == stage)
    return list(db.scalars(stmt).all())

@app.post("/api/v1/public/applications", response_model=ApplicationOut, status_code=201)
def create_public_application(payload: ApplicationCreate, db: Session = Depends(get_db)):
    """Prototype college intake. Public submissions are queued for authorised human review."""
    now = utcnow()
    application = Application(
        application_no=f"AICTE-{now:%Y%m%d%H%M%S}-{uuid4().hex[:4].upper()}",
        institution_name=payload.institution_name, institution_type=payload.institution_type,
        state=payload.state, application_type=payload.application_type,
        current_stage="Application Intake", priority="Normal", assigned_to=None,
        due_date=None, notes=payload.notes, status="Pending"
    )
    db.add(application); db.flush()
    seed_demo_documents(db, application, "Synthetic demo generator")
    db.add(WorkflowEvent(application_id=application.id, from_stage=None, to_stage=application.current_stage,
                         status=application.status, actor="College Portal", note="Public college application submitted; awaiting authorised manual review"))
    audit(db, "PUBLIC_APPLICATION_SUBMITTED", "College Portal", application.id,
          f"Application {application.application_no} submitted through the college portal")
    db.commit(); db.refresh(application)
    return application

@app.post("/api/v1/applications", response_model=ApplicationOut, status_code=201)
def create_application(payload: ApplicationCreate, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "officer"))):
    now = utcnow()
    application = Application(application_no=f"AICTE-{now:%Y%m%d%H%M%S}-{uuid4().hex[:4].upper()}",
        institution_name=payload.institution_name, institution_type=payload.institution_type, state=payload.state,
        application_type=payload.application_type, current_stage=payload.current_stage, priority=payload.priority,
        assigned_to=payload.assigned_to or user.full_name, due_date=payload.due_date, notes=payload.notes, status="Pending")
    db.add(application); db.flush()
    seed_demo_documents(db, application, "Synthetic demo generator")
    db.add(WorkflowEvent(application_id=application.id, from_stage=None, to_stage=application.current_stage, status=application.status, actor=user.full_name, note="Application registered"))
    audit(db, "APPLICATION_CREATED", user.full_name, application.id, f"Created {application.application_no}")
    db.commit(); db.refresh(application)
    return application

@app.get("/api/v1/applications/{application_id}", response_model=ApplicationOut)
def get_application(application_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles(*ALLOWED_ROLES))):
    item = db.get(Application, application_id)
    if not item: raise HTTPException(404, "Application not found")
    return item

@app.delete("/api/v1/applications/{application_id}", status_code=204)
def delete_application(application_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("admin"))):
    """Permanently remove an application and its application-scoped records.
    Restricted to admins; preserve a detached audit entry for the deletion itself.
    """
    item = db.get(Application, application_id)
    if not item:
        raise HTTPException(404, "Application not found")

    application_no = item.application_no
    institution_name = item.institution_name
    document_rows = db.scalars(select(DocumentRecord).where(
        DocumentRecord.application_id == item.id
    )).all()
    for document in document_rows:
        stored_path = Path(settings.upload_dir) / document.storage_key
        try:
            if stored_path.exists() and stored_path.is_file():
                stored_path.unlink()
        except OSError:
            # Do not abort database cleanup because a local file is unavailable.
            pass

    # Remove child records first to satisfy foreign-key constraints.
    for model in (ApplicationDocumentCheck, DocumentQualityCheck, DocumentValidity,
                  StageChecklistItem, EVCVisit, ComplianceItem, Undertaking,
                  DocumentRecord, WorkflowEvent, Alert, CollegeProfile):
        db.query(model).filter(model.application_id == item.id).delete(synchronize_session=False)
    db.query(AuditEvent).filter(AuditEvent.application_id == item.id).delete(synchronize_session=False)
    db.add(AuditEvent(
        application_id=None, action="APPLICATION_DELETED", actor=user.full_name,
        details=f"Deleted {application_no} — {institution_name} (application id {item.id}) by admin.",
    ))
    db.delete(item)
    db.commit()
    return None


@app.patch("/api/v1/applications/{application_id}", response_model=ApplicationOut)
def update_application(application_id: int, payload: ApplicationUpdate, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "officer"))):
    item = db.get(Application, application_id)
    if not item: raise HTTPException(404, "Application not found")
    changes = payload.model_dump(exclude_unset=True)
    for key, value in changes.items(): setattr(item, key, value)
    audit(db, "APPLICATION_UPDATED", user.full_name, item.id, ", ".join(changes.keys()))
    db.commit(); db.refresh(item)
    return item

def try_auto_route_to_evc(db: Session, application: Application, actor: str) -> bool:
    """Route a scrutiny-approved application to the EVC queue once every required
    document checklist item is verified. This creates a queue item, not a visit
    appointment; no date or panel is fabricated.
    """
    if application.current_stage not in {"Scrutiny", "Re-scrutiny"}:
        return False
    required_checks = db.scalars(select(ApplicationDocumentCheck).where(
        ApplicationDocumentCheck.application_id == application.id,
        ApplicationDocumentCheck.required.is_(True),
    )).all()
    if not required_checks or any(check.status not in {"Verified", "Not Applicable"} for check in required_checks):
        return False
    quality_issues = db.scalars(select(DocumentQualityCheck).where(
        DocumentQualityCheck.application_id == application.id,
        DocumentQualityCheck.status == "Needs Correction",
    )).all()
    if quality_issues:
        return False
    stages = stages_for_application(db, application.application_type)
    operational_stages = [stage for stage in stages if stage not in {"Re-scrutiny", "Returned to Institution", "Approved / Closed", "Rejected"}]
    reference_stage = "Scrutiny" if application.current_stage == "Re-scrutiny" else application.current_stage
    if reference_stage not in operational_stages:
        return False
    current_index = operational_stages.index(reference_stage)
    if current_index + 1 >= len(operational_stages) or operational_stages[current_index + 1] != "EVC Scheduling":
        return False
    old_stage = application.current_stage
    application.current_stage = "EVC Scheduling"
    application.status = "In Review"
    evc_visit = db.scalar(select(EVCVisit).where(
        EVCVisit.application_id == application.id,
        EVCVisit.status.not_in(["Completed", "Cancelled"]),
    ).limit(1))
    if not evc_visit:
        db.add(EVCVisit(
            application_id=application.id,
            proposed_date=None,
            location="Institution campus",
            panel_members=None,
            status="Proposed",
        ))
    evc_officer = db.scalar(select(User).where(
        User.role == "evc_officer", User.active.is_(True)
    ).order_by(User.id))
    if evc_officer:
        application.assigned_to = evc_officer.full_name
    db.add(WorkflowEvent(
        application_id=application.id, from_stage=old_stage, to_stage=application.current_stage,
        status=application.status, actor=actor,
        note="All required document checklist items verified; automatically routed to EVC queue. Visit scheduling remains pending.",
    ))
    audit(db, "AUTOMATIC_EVC_HANDOFF", actor, application.id,
          f"All required document checklist items verified; moved from {old_stage} to EVC Scheduling.")
    return True


@app.post("/api/v1/applications/{application_id}/transition", response_model=ApplicationOut)
def transition_application(application_id: int, payload: TransitionIn, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "reviewer", "scrutiny_officer", "evc_officer", "sac_shc_officer", "compliance_officer", "final_approval_officer"))):
    item = db.get(Application, application_id)
    if not item: raise HTTPException(404, "Application not found")
    stage_roles = {
        "Application Intake": "scrutiny_officer", "Scrutiny": "scrutiny_officer", "Re-scrutiny": "scrutiny_officer",
        "EVC Scheduling": "evc_officer", "EVC Visit": "evc_officer",
        "SAC/SHC Review": "sac_shc_officer", "Compliance": "compliance_officer",
    }
    stages = stages_for_application(db, item.application_type)
    if payload.status == "Stage Completed":
        if payload.to_stage != item.current_stage:
            raise HTTPException(422, "Select the current stage to complete it; the system will route it to the next stage.")
        if item.current_stage in {"Application Intake", "Scrutiny", "Re-scrutiny", "Returned to Institution"}:
            quality_issues = db.scalars(select(DocumentQualityCheck).where(
                DocumentQualityCheck.application_id == item.id,
                DocumentQualityCheck.status == "Needs Correction"
            )).all()
            if quality_issues:
                raise HTTPException(409, "Document correction issues must be resolved before the application can advance.")
        outstanding = db.scalars(select(StageChecklistItem).where(
            StageChecklistItem.application_id == item.id,
            StageChecklistItem.stage.in_([item.current_stage, "Final Approval"]) if item.current_stage == "Compliance" else StageChecklistItem.stage == item.current_stage,
            StageChecklistItem.required.is_(True),
        )).all()
        outstanding = [entry for entry in outstanding if entry.status not in {"Verified", "Not Applicable"}]
        if outstanding:
            labels = ", ".join(entry.title for entry in outstanding[:5])
            raise HTTPException(409, f"Required {item.current_stage} checklist items are outstanding: {labels}")
        operational_for_completion = [stage for stage in stages if stage not in {"Re-scrutiny", "Returned to Institution", "Approved / Closed", "Rejected"}]
        if item.current_stage == "Returned to Institution":
            next_stage = "Re-scrutiny" if "Re-scrutiny" in stages else ("Scrutiny" if "Scrutiny" in operational_for_completion else None)
        elif item.current_stage == "Re-scrutiny":
            scrutiny_index = operational_for_completion.index("Scrutiny") if "Scrutiny" in operational_for_completion else -1
            next_stage = operational_for_completion[scrutiny_index + 1] if 0 <= scrutiny_index + 1 < len(operational_for_completion) else None
        elif item.current_stage in operational_for_completion:
            current_index = operational_for_completion.index(item.current_stage)
            next_stage = operational_for_completion[current_index + 1] if current_index + 1 < len(operational_for_completion) else ("Approved / Closed" if "Approved / Closed" in stages else None)
        else:
            next_stage = None
        if not next_stage:
            raise HTTPException(422, "No next stage is configured. Ask an administrator to review the workflow route.")
        payload.to_stage = next_stage
        payload.status = "Completed" if next_stage == "Approved / Closed" else "In Review"
    required_role = "final_approval_officer" if payload.to_stage in {"Approved / Closed", "Rejected"} else stage_roles.get(item.current_stage)
    if user.role not in {"admin", "reviewer"} and user.role != required_role:
        raise HTTPException(403, "This transition is not permitted for your role.")
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
    ensure_stage_checklist(db, item, item.current_stage)

    # End-to-end routing: after an authorised transition, route the open case to
    # the active owner of its new stage and create a deduplicated in-app handoff.
    # The system never makes the statutory decision itself.
    next_stage_roles = {
        "Application Intake": "scrutiny_officer",
        "Scrutiny": "scrutiny_officer",
        "Re-scrutiny": "scrutiny_officer",
        "EVC Scheduling": "evc_officer",
        "EVC Visit": "evc_officer",
        "SAC/SHC Review": "sac_shc_officer",
        "Compliance": "compliance_officer",
        "Final Approval": "final_approval_officer",
    }
    if item.current_stage in TERMINAL_STAGES:
        item.assigned_to = None
    elif item.current_stage == "Returned to Institution":
        # The college must correct the submission before it returns to scrutiny.
        item.assigned_to = None
        title = "Action required: application returned for correction"
        exists = db.scalar(select(Alert.id).where(
            Alert.application_id == item.id, Alert.category == "Workflow Handoff",
            Alert.title == title, Alert.acknowledged.is_(False),
        ))
        if not exists:
            db.add(Alert(application_id=item.id, title=title,
                         message=f"{item.application_no} was returned to the institution. Review the recorded reason, correct the submission, and resubmit through the College Portal.",
                         severity="High", category="Workflow Handoff"))
    else:
        target_role = next_stage_roles.get(item.current_stage)
        target_user = db.scalar(
            select(User).where(User.role == target_role, User.active.is_(True)).order_by(User.id)
        ) if target_role else None
        if target_user:
            item.assigned_to = target_user.full_name
            title = "Application assigned to your stage"
            exists = db.scalar(select(Alert.id).where(
                Alert.application_id == item.id, Alert.category == "Workflow Handoff",
                Alert.title == title, Alert.message.contains(target_user.username),
                Alert.acknowledged.is_(False),
            ))
            if not exists:
                db.add(Alert(application_id=item.id, title=title,
                             message=f"{item.application_no} is now at {item.current_stage} and assigned to {target_user.full_name} ({target_user.username}). Please review the case and required checklist.",
                             severity="Medium", category="Workflow Handoff"))
            audit(db, "AUTOMATION_CASE_ASSIGNED", "system", item.id,
                  f"Assigned to {target_user.username} for stage {item.current_stage} after transition.")
        else:
            item.assigned_to = None
            title = "Workflow handoff requires configuration"
            exists = db.scalar(select(Alert.id).where(
                Alert.application_id == item.id, Alert.category == "Workflow Handoff",
                Alert.title == title, Alert.acknowledged.is_(False),
            ))
            if not exists:
                db.add(Alert(application_id=item.id, title=title,
                             message=f"{item.application_no} reached '{item.current_stage}', but no active authorised stage owner is configured. An administrator must configure the owner before work continues.",
                             severity="High", category="Workflow Handoff"))
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
    now = utcnow()
    validity_rows = db.scalars(select(DocumentValidity).where(
        DocumentValidity.status.not_in(["Renewed", "Not Applicable"])
    )).all()
    documents_expired = sum(
        1 for row in validity_rows
        if row.expiry_date is not None and safe_dt(row.expiry_date) < now
    )
    documents_expiring_30_days = sum(
        1 for row in validity_rows
        if row.expiry_date is not None and now <= safe_dt(row.expiry_date) <= now + timedelta(days=30)
    )
    return {"total_applications": total, "pending_applications": pending, "overdue_applications": overdue,
            "evc_upcoming": evc_upcoming, "compliance_open": compliance_open, "alerts_open": alerts_open,
            "documents_expired": documents_expired, "documents_expiring_30_days": documents_expiring_30_days,
            "stage_counts": [{"stage": stage, "count": count} for stage, count in stage_rows]}

def _effective_compliance_status(item: ComplianceItem) -> str:
    if item.status not in {"Compliant", "Non-Compliant"} and item.due_date and safe_dt(item.due_date) < utcnow():
        return "Overdue"
    if item.status in {"Verified", "Submitted"}:
        return "Compliant" if item.status == "Verified" else "Under Review"
    if item.status in {"Rejected", "Expired"}:
        return "Non-Compliant" if item.status == "Rejected" else "Overdue"
    return item.status if item.status in {"Pending", "Under Review", "Compliant", "Non-Compliant", "Overdue"} else "Pending"

def _compliance_out(item: ComplianceItem, db: Session) -> dict:
    application = db.get(Application, item.application_id)
    return {"id": item.id, "application_id": item.application_id,
        "application_no": application.application_no if application else "",
        "institution_name": application.institution_name if application else "",
        "title": item.title, "category": item.category, "due_date": item.due_date,
        "status": _effective_compliance_status(item), "assigned_to": item.assigned_to,
        "notes": item.notes, "updated_at": item.updated_at, "created_at": item.created_at}

def sync_recorded_compliance_tasks(db: Session) -> int:
    """Create idempotent tasks only from explicitly recorded validity and undertaking data."""
    created = 0
    validity_rows = db.scalars(select(DocumentValidity)).all()
    for record in validity_rows:
        if not record.expiry_date and not record.renewal_due_date:
            continue
        title = f"Review validity: {record.document_name}"
        exists = db.scalar(select(ComplianceItem.id).where(
            ComplianceItem.application_id == record.application_id,
            ComplianceItem.title == title))
        if exists:
            continue
        due = record.renewal_due_date or record.expiry_date
        task = ComplianceItem(application_id=record.application_id, title=title,
            category="Document Validity", due_date=due, status="Pending",
            assigned_to=record.updated_by or None,
            notes=f"Generated from recorded document validity metadata (record #{record.id}). Review the linked validity record; expiry is not itself a compliance decision.")
        db.add(task); db.flush()
        db.add(ComplianceStatusHistory(compliance_item_id=task.id, from_status=None, to_status="Pending",
            note=f"Generated from document validity record #{record.id}.", changed_by="system"))
        created += 1
    undertaking_rows = db.scalars(select(Undertaking)).all()
    for undertaking in undertaking_rows:
        if undertaking.submitted:
            continue
        title = f"Pending undertaking: {undertaking.title}"
        exists = db.scalar(select(ComplianceItem.id).where(
            ComplianceItem.application_id == undertaking.application_id,
            ComplianceItem.title == title))
        if exists:
            continue
        task = ComplianceItem(application_id=undertaking.application_id, title=title,
            category="Undertaking", due_date=undertaking.due_date, status="Pending",
            notes=f"Generated from pending undertaking record #{undertaking.id}. " +
                  ("Deadline taken from the recorded undertaking." if undertaking.due_date else "No deadline is recorded; manual review required."))
        db.add(task); db.flush()
        db.add(ComplianceStatusHistory(compliance_item_id=task.id, from_status=None, to_status="Pending",
            note=f"Generated from undertaking record #{undertaking.id}.", changed_by="system"))
        created += 1
    if created:
        db.commit()
    return created

@app.get("/api/v1/compliance")
def list_compliance(application_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(require_roles(*ALLOWED_ROLES))):
    sync_recorded_compliance_tasks(db)
    stmt = select(ComplianceItem).order_by(ComplianceItem.due_date.asc())
    if application_id is not None: stmt = stmt.where(ComplianceItem.application_id == application_id)
    rows = db.scalars(stmt).all()
    now = utcnow()
    for item in rows:
        effective = _effective_compliance_status(item)
        if effective == "Overdue" and item.status not in {"Overdue", "Compliant", "Non-Compliant"}:
            item.status = "Overdue"; item.updated_at = now
            db.add(ComplianceStatusHistory(compliance_item_id=item.id, from_status=None, to_status="Overdue",
                note="Automatically marked overdue because the due date passed while unresolved.", changed_by="system"))
    db.commit()
    return [_compliance_out(r, db) for r in rows]

@app.post("/api/v1/compliance", status_code=201)
def create_compliance(payload: ComplianceCreate, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "officer", "compliance_officer"))):
    app_item = db.get(Application, payload.application_id)
    if not app_item: raise HTTPException(404, "Application not found")
    if user.role == "compliance_officer" and app_item.current_stage != "Compliance": raise HTTPException(403, "Compliance officers can create tasks only for applications in Compliance.")
    title = payload.title.strip()
    if db.scalar(select(ComplianceItem.id).where(ComplianceItem.application_id == app_item.id, ComplianceItem.title == title)):
        raise HTTPException(409, "A compliance task with this title already exists for the application.")
    if payload.category not in {"Document Validity", "Undertaking", "Infrastructure", "Faculty", "Approval Condition", "Other", "Document validity", "Faculty compliance", "Regulatory condition"}:
        raise HTTPException(422, "Select a supported compliance category.")
    item = ComplianceItem(application_id=app_item.id, title=title, category=payload.category,
        due_date=payload.due_date, assigned_to=payload.assigned_to or user.full_name, notes=payload.notes, status="Pending")
    db.add(item); db.flush()
    db.add(ComplianceStatusHistory(compliance_item_id=item.id, from_status=None, to_status="Pending", note="Task created.", changed_by=user.full_name))
    audit(db, "COMPLIANCE_CREATED", user.full_name, item.application_id, item.title)
    db.commit(); db.refresh(item)
    return _compliance_out(item, db)

@app.patch("/api/v1/compliance/{item_id}")
def update_compliance(item_id: int, payload: ComplianceUpdate, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "officer", "reviewer", "compliance_officer"))):
    item = db.get(ComplianceItem, item_id)
    if not item: raise HTTPException(404, "Compliance item not found")
    app_item = db.get(Application, item.application_id)
    if user.role == "compliance_officer" and (not app_item or app_item.current_stage != "Compliance"): raise HTTPException(403, "Compliance officers can update items only while the application is in Compliance.")
    changes = payload.model_dump(exclude_unset=True)
    new_status = changes.get("status")
    allowed = {"Pending", "Under Review", "Compliant", "Non-Compliant", "Overdue"}
    if new_status is not None and new_status not in allowed:
        raise HTTPException(422, "Unsupported compliance status.")
    if new_status == "Non-Compliant" and not (changes.get("notes") or item.notes or "").strip():
        raise HTTPException(422, "Add an explanation in notes before marking a task Non-Compliant.")
    old_status = _effective_compliance_status(item)
    for key, value in changes.items(): setattr(item, key, value)
    if "due_date" in changes and item.due_date and safe_dt(item.due_date) < utcnow() and item.status not in {"Compliant", "Non-Compliant"}:
        item.status = "Overdue"
    item.updated_at = utcnow()
    if item.status != old_status:
        db.add(ComplianceStatusHistory(compliance_item_id=item.id, from_status=old_status, to_status=item.status,
            note=item.notes, changed_by=user.full_name))
    audit(db, "COMPLIANCE_UPDATED", user.full_name, item.application_id, f"{item.title}: {old_status} -> {item.status}")
    db.commit(); db.refresh(item)
    return _compliance_out(item, db)

@app.get("/api/v1/compliance/{item_id}/history")
def compliance_history(item_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles(*ALLOWED_ROLES))):
    if not db.get(ComplianceItem, item_id): raise HTTPException(404, "Compliance item not found")
    rows = db.scalars(select(ComplianceStatusHistory).where(ComplianceStatusHistory.compliance_item_id == item_id).order_by(ComplianceStatusHistory.changed_at.desc())).all()
    return [{"from_status": r.from_status, "to_status": r.to_status, "note": r.note, "changed_by": r.changed_by, "changed_at": r.changed_at} for r in rows]

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
def create_document_validity(payload: DocumentValidityCreate, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "officer", "compliance_officer"))):
    application = db.get(Application, payload.application_id)
    if not application:
        raise HTTPException(404, "Application not found")
    if user.role == "compliance_officer" and application.current_stage != "Compliance": raise HTTPException(403, "Compliance officers can add validity records only during Compliance.")
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
def update_document_validity(item_id: int, payload: DocumentValidityUpdate, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "officer", "reviewer", "compliance_officer"))):
    item = db.get(DocumentValidity, item_id)
    if not item:
        raise HTTPException(404, "Document validity record not found")
    app_item = db.get(Application, item.application_id)
    if user.role == "compliance_officer" and (not app_item or app_item.current_stage != "Compliance"): raise HTTPException(403, "Compliance officers can update validity records only during Compliance.")
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
def create_undertaking(payload: UndertakingCreate, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "officer", "compliance_officer"))):
    app_item = db.get(Application, payload.application_id)
    if not app_item: raise HTTPException(404, "Application not found")
    if user.role == "compliance_officer" and app_item.current_stage != "Compliance": raise HTTPException(403, "Compliance officers can create undertakings only during Compliance.")
    item = Undertaking(**payload.model_dump()); db.add(item); db.flush()
    audit(db, "UNDERTAKING_CREATED", user.full_name, item.application_id, item.title); db.commit(); db.refresh(item)
    return {"id": item.id, "application_id": item.application_id, "title": item.title, "submitted": item.submitted, "due_date": item.due_date, "notes": item.notes}

@app.patch("/api/v1/undertakings/{item_id}")
def update_undertaking(item_id: int, payload: UndertakingUpdate, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "officer", "compliance_officer"))):
    item = db.get(Undertaking, item_id)
    if not item: raise HTTPException(404, "Undertaking not found")
    app_item = db.get(Application, item.application_id)
    if user.role == "compliance_officer" and (not app_item or app_item.current_stage != "Compliance"): raise HTTPException(403, "Compliance officers can update undertakings only during Compliance.")
    for key, value in payload.model_dump(exclude_unset=True).items(): setattr(item, key, value)
    audit(db, "UNDERTAKING_UPDATED", user.full_name, item.application_id, item.title); db.commit(); db.refresh(item)
    return {"id": item.id, "application_id": item.application_id, "title": item.title, "submitted": item.submitted, "due_date": item.due_date, "notes": item.notes}

@app.get("/api/v1/evc")
def list_evc(db: Session = Depends(get_db), user: User = Depends(require_roles(*ALLOWED_ROLES))):
    rows = db.scalars(select(EVCVisit).order_by(EVCVisit.proposed_date.asc())).all()
    return [{"id": x.id, "application_id": x.application_id, "application_no": db.get(Application, x.application_id).application_no if db.get(Application, x.application_id) else "", "institution_name": db.get(Application, x.application_id).institution_name if db.get(Application, x.application_id) else "", "proposed_date": x.proposed_date, "location": x.location, "panel_members": x.panel_members, "status": x.status, "outcome": x.outcome} for x in rows]

@app.post("/api/v1/evc", status_code=201)
def create_evc(payload: EVCVisitCreate, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "officer", "evc_officer"))):
    app_item = db.get(Application, payload.application_id)
    if not app_item: raise HTTPException(404, "Application not found")
    if user.role == "evc_officer" and app_item.current_stage not in {"EVC Scheduling", "EVC Visit"}: raise HTTPException(403, "EVC officers can schedule visits only for applications in the EVC stages.")
    item = EVCVisit(**payload.model_dump(), status="Proposed"); db.add(item); db.flush()
    audit(db, "EVC_VISIT_CREATED", user.full_name, item.application_id, f"Visit proposed for {payload.proposed_date}")
    db.commit(); db.refresh(item)
    return {"id": item.id, "application_id": item.application_id, "proposed_date": item.proposed_date, "location": item.location, "panel_members": item.panel_members, "status": item.status, "outcome": item.outcome}

@app.patch("/api/v1/evc/{visit_id}")
def update_evc(visit_id: int, payload: EVCVisitUpdate, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "officer", "reviewer", "evc_officer"))):
    item = db.get(EVCVisit, visit_id)
    if not item: raise HTTPException(404, "EVC visit not found")
    app_item = db.get(Application, item.application_id)
    if user.role == "evc_officer" and (not app_item or app_item.current_stage not in {"EVC Scheduling", "EVC Visit"}): raise HTTPException(403, "EVC officers can update visits only while the application is in an EVC stage.")
    for key, value in payload.model_dump(exclude_unset=True).items(): setattr(item, key, value)
    audit(db, "EVC_VISIT_UPDATED", user.full_name, item.application_id, f"Status: {item.status}; outcome: {item.outcome or ''}")

    # When the EVC visit is completed, route to the next configured operational
    # stage for this application type. Never hardcode SAC/SHC as the next stage.
    if app_item and str(item.status).strip().lower() in {"completed", "complete"} and app_item.current_stage in {"EVC Scheduling", "EVC Visit"}:
        stages = stages_for_application(db, app_item.application_type)
        operational_stages = [stage for stage in stages if stage not in {"Re-scrutiny", "Returned to Institution", "Approved / Closed", "Rejected"}]
        if "EVC Visit" not in operational_stages:
            raise HTTPException(422, "EVC Visit is not present in this application's configured workflow route.")
        evc_index = operational_stages.index("EVC Visit")
        if evc_index + 1 >= len(operational_stages):
            raise HTTPException(422, "No next operational stage is configured after EVC Visit. Ask an administrator to review the workflow route.")
        next_stage = operational_stages[evc_index + 1]
        if next_stage in {"Approved / Closed", "Rejected"}:
            raise HTTPException(409, "EVC is complete, but the configured next step is a terminal decision. An authorised officer must record that decision explicitly.")
        old_stage = app_item.current_stage
        # Do not repeat a handoff if the same visit is updated to Completed again.
        already_handed_off = db.scalar(select(WorkflowEvent.id).where(
            WorkflowEvent.application_id == app_item.id,
            WorkflowEvent.from_stage == old_stage,
            WorkflowEvent.to_stage == next_stage,
            WorkflowEvent.note.like("EVC visit marked complete%"),
        ))
        if not already_handed_off:
            app_item.current_stage = next_stage
            app_item.status = "In Review"
            ensure_stage_checklist(db, app_item, next_stage)
            next_stage_roles = {
                "Application Intake": "scrutiny_officer", "Scrutiny": "scrutiny_officer",
                "Re-scrutiny": "scrutiny_officer", "EVC Scheduling": "evc_officer",
                "EVC Visit": "evc_officer", "SAC/SHC Review": "sac_shc_officer",
                "Compliance": "compliance_officer", "Final Approval": "final_approval_officer",
            }
            next_role = next_stage_roles.get(next_stage)
            next_officer = db.scalar(select(User).where(
                User.role == next_role, User.active.is_(True)
            ).order_by(User.id)) if next_role else None
            if next_officer:
                app_item.assigned_to = next_officer.full_name
            db.add(WorkflowEvent(
                application_id=app_item.id, from_stage=old_stage, to_stage=next_stage,
                status=app_item.status, actor=user.full_name,
                note=f"EVC visit marked complete; automatically handed off to {next_stage}",
            ))
            audit(db, "WORKFLOW_STAGE_HANDOFF", user.full_name, app_item.id,
                  f"EVC visit completed; moved from {old_stage} to configured next stage {next_stage}")
    db.commit(); db.refresh(item); db.refresh(app_item) if app_item else None
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
def update_document_check(check_id: int, payload: DocumentCheckUpdate, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "officer", "reviewer", "scrutiny_officer"))):
    check = db.get(ApplicationDocumentCheck, check_id)
    if not check:
        raise HTTPException(404, "Checklist item not found")
    app_item = db.get(Application, check.application_id)
    if user.role == "scrutiny_officer" and (not app_item or app_item.current_stage not in {"Application Intake", "Scrutiny", "Re-scrutiny", "Returned to Institution"}): raise HTTPException(403, "Scrutiny officers can update intake checklist items only during scrutiny stages.")
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
    auto_routed_to_evc = bool(app_item and try_auto_route_to_evc(db, app_item, user.full_name))
    db.commit()
    response = {"id": check.id, "status": check.status, "document_id": check.document_id, "updated_by": check.updated_by, "updated_at": check.updated_at}
    response["automatic_evc_handoff"] = auto_routed_to_evc
    response["application_stage"] = app_item.current_stage if app_item else None
    return response

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
