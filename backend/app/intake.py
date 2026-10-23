import json
import os
import re
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from PIL import Image, ImageFilter, ImageStat
from pypdf import PdfReader

from app.config import settings
from app.database import get_db
from app.models import Alert, Application, AuditEvent, CollegeProfile, DocumentQualityCheck, DocumentRecord, StageChecklistItem, User, WorkflowEvent
from app.security import current_user, hash_password

router = APIRouter()
MAX_UPLOAD_BYTES = settings.max_upload_mb * 1024 * 1024
COLLEGE_DOCUMENTS = (
    ("registration_document", "Institution Registration / Approval Proof"),
    ("land_building_document", "Land / Building Evidence"),
    ("faculty_infrastructure_document", "Faculty / Infrastructure Details"),
)
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}


def _audit(db: Session, action: str, actor: str, application_id: int | None, details: str) -> None:
    db.add(AuditEvent(application_id=application_id, action=action, actor=actor, details=details))


def _analyse_document(slot_name: str, filename: str, content: bytes, institution_name: str) -> list[str]:
    findings: list[str] = []
    suffix = Path(filename).suffix.lower()
    if not content:
        return ["The uploaded file is empty."]
    if suffix in {".png", ".jpg", ".jpeg"}:
        try:
            with Image.open(BytesIO(content)) as image:
                width, height = image.size
                if width < 1000 or height < 700:
                    findings.append(f"Low image resolution ({width}×{height}); upload a clearer scan.")
                grayscale_stats = ImageStat.Stat(image.convert("L"))
                edge_stats = ImageStat.Stat(image.convert("L").filter(ImageFilter.FIND_EDGES))
                if grayscale_stats.var[0] < 25 or edge_stats.var[0] < 25:
                    findings.append("Image appears blurred or low-contrast; upload a sharper, readable scan.")
        except Exception:
            findings.append("Image could not be decoded; upload a valid PNG or JPEG file.")
    elif suffix == ".pdf":
        try:
            reader = PdfReader(BytesIO(content), strict=False)
            if not reader.pages:
                findings.append("PDF contains no readable pages.")
            extracted = " ".join((page.extract_text() or "") for page in reader.pages).strip()
            if len(extracted) < 40:
                findings.append("PDF has little or no extractable text; it may be scanned or unreadable. Upload a text-readable PDF or ensure a human reviewer can read it.")
            else:
                if institution_name.casefold() not in extracted.casefold():
                    findings.append("Institution name was not found in extracted text; verify that this is the correct institution's document.")
                if not re.search(r"\b(?:19|20)\d{2}\b|\b[A-Z]{1,8}[-/]?\d{3,}\b", extracted, re.I):
                    findings.append("No obvious date, year, or reference number was found; verify minor document details manually.")
        except Exception:
            findings.append("PDF could not be parsed; upload a valid, readable PDF.")
    return findings


def _send_correction_email(recipient: str, application_no: str, institution_name: str, issues: list[str]) -> bool:
    host = os.getenv("SMTP_HOST", "").strip()
    sender = os.getenv("SMTP_FROM", "").strip()
    if not host or not sender:
        return False
    message = EmailMessage()
    message["Subject"] = f"Action required: documents need correction for {application_no}"
    message["From"] = sender
    message["To"] = recipient
    issue_lines = "\n".join(f"- {issue}" for issue in issues)
    message.set_content(
        f"Dear {institution_name},\n\n"
        f"Automated preliminary checks found possible issues in your AICTE WorkflowOS submission ({application_no}):\n"
        f"{issue_lines}\n\n"
        "Please sign in to the College Portal and upload corrected documents. These checks are preliminary and do not constitute an official compliance decision.\n"
    )
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    use_ssl = os.getenv("SMTP_USE_SSL", "").lower() in {"1", "true", "yes"}
    if use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=10) as server:
            if username:
                server.login(username, password)
            server.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=10) as server:
            server.ehlo()
            if os.getenv("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes"}:
                server.starttls()
                server.ehlo()
            if username:
                server.login(username, password)
            server.send_message(message)
    return True


@router.post("/api/v1/public/college-submissions", status_code=201)
async def create_college_submission(
    institution_name: str = Form(..., min_length=2, max_length=240),
    institution_type: str = Form("Technical Institution", max_length=80),
    state: str = Form(..., min_length=2, max_length=100),
    application_type: str = Form("New Institution", max_length=120),
    email: str = Form(..., max_length=254),
    password: str = Form(..., min_length=10, max_length=128),
    registration_document: UploadFile = File(...),
    land_building_document: UploadFile = File(...),
    faculty_infrastructure_document: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Create a college account and an application with exactly three required intake documents."""
    email = email.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(422, "Enter a valid institutional email address")
    if db.scalar(select(User.id).where(User.username == email)):
        raise HTTPException(409, "An account already exists for this email. Sign in to the College Portal instead.")
    files = [
        ("registration_document", "Institution Registration / Approval Proof", registration_document),
        ("land_building_document", "Land / Building Evidence", land_building_document),
        ("faculty_infrastructure_document", "Faculty / Infrastructure Details", faculty_infrastructure_document),
    ]
    prepared = []
    for slot_key, slot_label, upload in files:
        safe_name = Path(upload.filename or "").name
        if not safe_name or Path(safe_name).suffix.lower() not in ALLOWED_EXTENSIONS:
            raise HTTPException(415, f"{slot_label}: upload a PDF, PNG or JPEG file")
        data = await upload.read(MAX_UPLOAD_BYTES + 1)
        if not data:
            raise HTTPException(422, f"{slot_label}: file is empty")
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"{slot_label}: file exceeds the {settings.max_upload_mb} MB limit")
        prepared.append((slot_key, slot_label, safe_name, upload.content_type or "application/octet-stream", data))

    now = datetime.now(timezone.utc)
    application = Application(
        application_no=f"AICTE-{now:%Y%m%d%H%M%S}-{uuid4().hex[:4].upper()}",
        institution_name=institution_name.strip(), institution_type=institution_type.strip(),
        state=state.strip(), application_type=application_type.strip(),
        current_stage="Application Intake", status="Pending", priority="Normal",
        assigned_to=None, notes=f"Submitted through authenticated College Portal account: {email}",
    )
    college = User(username=email, full_name=institution_name.strip(), role="college",
                   password_hash=hash_password(password), active=True)
    db.add(college)
    db.add(application)
    db.flush()
    db.add(CollegeProfile(user_id=college.id, application_id=application.id,
                          institution_name=institution_name.strip(), contact_email=email))
    all_issues: list[str] = []
    for slot_key, slot_label, filename, content_type, data in prepared:
        storage_key = uuid4().hex + Path(filename).suffix.lower()
        target = Path(settings.upload_dir) / storage_key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        record = DocumentRecord(application_id=application.id, original_name=filename,
                                storage_key=storage_key, content_type=content_type,
                                size_bytes=len(data), uploaded_by=institution_name.strip())
        db.add(record)
        db.flush()
        findings = _analyse_document(slot_label, filename, data, institution_name.strip())
        is_image = Path(filename).suffix.lower() in {".png", ".jpg", ".jpeg"}
        status = "Needs Correction" if findings else ("Manual Review Required" if is_image else "Preliminary Checks Passed")
        db.add(DocumentQualityCheck(document_id=record.id, application_id=application.id,
                                    slot_name=slot_label, status=status, findings_json=json.dumps(findings)))
        if findings:
            all_issues.extend([f"{slot_label}: {finding}" for finding in findings])
    db.add(WorkflowEvent(application_id=application.id, from_stage=None, to_stage="Application Intake",
                         status="Pending", actor=institution_name.strip(),
                         note="College account and submission created with three required documents. Automated checks are preliminary."))
    _audit(db, "COLLEGE_ACCOUNT_CREATED", institution_name.strip(), application.id, f"College account created for {email}")
    _audit(db, "COLLEGE_APPLICATION_SUBMITTED", institution_name.strip(), application.id,
           f"{application.application_no}; exactly three required documents stored")
    email_sent = False
    if all_issues:
        correction_alert = Alert(application_id=application.id, title="Document correction required",
                                 message=f"{application.application_no}: preliminary document checks found {len(all_issues)} issue(s).",
                                 severity="High", category="Document Quality")
        db.add(correction_alert)
        try:
            email_sent = _send_correction_email(email, application.application_no, institution_name.strip(), all_issues)
        except Exception as exc:
            _audit(db, "CORRECTION_EMAIL_FAILED", "system", application.id, f"SMTP delivery failed: {type(exc).__name__}")
            correction_alert.message += " Correction email delivery failed; review the audit trail and contact the institution through an approved channel."
        else:
            if email_sent:
                _audit(db, "CORRECTION_EMAIL_SENT", "system", application.id, f"Correction notice sent to {email}")
                correction_alert.message += " A correction email was sent."
            else:
                correction_alert.message += " Email delivery is not configured; contact details are recorded for follow-up."
    db.commit()
    return {
        "application_id": application.id,
        "application_no": application.application_no,
        "institution_name": application.institution_name,
        "status": application.status,
        "document_count": len(prepared),
        "document_checks": [{"slot": slot_label, "status": "Needs Correction" if any(issue.startswith(slot_label + ":") for issue in all_issues) else ("Manual Review Required" if Path(filename).suffix.lower() in {".png", ".jpg", ".jpeg"} else "Preliminary Checks Passed")} for _, slot_label, filename, _, _ in prepared],
        "issues": all_issues,
        "correction_email_sent": email_sent,
        "email_delivery_configured": bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_FROM")),
    }


@router.get("/api/v1/college/application")
def get_college_application(db: Session = Depends(get_db), user: User = Depends(current_user)):
    if user.role != "college":
        raise HTTPException(403, "College account required")
    profile = db.scalar(select(CollegeProfile).where(CollegeProfile.user_id == user.id))
    if not profile:
        raise HTTPException(404, "No college application is linked to this account")
    application = db.get(Application, profile.application_id)
    if not application:
        raise HTTPException(404, "Application not found")
    docs = db.scalars(select(DocumentRecord).where(DocumentRecord.application_id == application.id).order_by(DocumentRecord.id)).all()
    quality = {item.document_id: item for item in db.scalars(select(DocumentQualityCheck).where(DocumentQualityCheck.application_id == application.id)).all()}
    return {
        "application": {
            "id": application.id, "application_no": application.application_no,
            "institution_name": application.institution_name, "institution_type": application.institution_type,
            "state": application.state, "application_type": application.application_type,
            "current_stage": application.current_stage, "status": application.status,
            "submitted_at": application.submitted_at, "notes": application.notes,
        },
        "documents": [{
            "id": doc.id, "name": doc.original_name, "size_bytes": doc.size_bytes,
            "slot": quality[doc.id].slot_name if doc.id in quality else "Document",
            "status": quality[doc.id].status if doc.id in quality else "Needs Review",
            "findings": json.loads(quality[doc.id].findings_json) if doc.id in quality else ["No quality result available"],
        } for doc in docs],
        "note": "Automated checks are preliminary heuristics. Human officers must verify authenticity and regulatory sufficiency.",
    }


@router.get("/api/v1/officer/queue")
def get_officer_queue(db: Session = Depends(get_db), user: User = Depends(current_user)):
    stage_access = {
        "scrutiny_officer": {"Application Intake", "Scrutiny", "Re-scrutiny"},
        "evc_officer": {"EVC Scheduling", "EVC Visit"},
        "sac_shc_officer": {"SAC/SHC Review"},
        "compliance_officer": {"Compliance"},
        "final_approval_officer": {"Compliance"},
    }
    if user.role not in stage_access:
        raise HTTPException(403, "A stage-specific officer account is required")
    stages = stage_access[user.role]
    rows = db.scalars(select(Application).where(Application.current_stage.in_(stages)).order_by(Application.submitted_at.desc())).all()
    return {"role": user.role, "stages": sorted(stages), "applications": [{
        "id": row.id, "application_no": row.application_no, "institution_name": row.institution_name,
        "application_type": row.application_type, "current_stage": row.current_stage, "status": row.status,
        "priority": row.priority, "state": row.state, "submitted_at": row.submitted_at,
    } for row in rows]}


@router.get("/api/v1/applications/{application_id}/document-quality")
def get_application_document_quality(application_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if user.role not in {"admin", "officer", "reviewer", "auditor", "scrutiny_officer", "evc_officer", "sac_shc_officer", "compliance_officer", "final_approval_officer"}:
        raise HTTPException(403, "Authorised AICTE staff role required")
    if not db.get(Application, application_id):
        raise HTTPException(404, "Application not found")
    checks = db.scalars(select(DocumentQualityCheck).where(DocumentQualityCheck.application_id == application_id).order_by(DocumentQualityCheck.id)).all()
    return [{
        "document_id": check.document_id, "slot": check.slot_name, "status": check.status,
        "findings": json.loads(check.findings_json), "checked_at": check.checked_at,
    } for check in checks]


class StageChecklistCreate(BaseModel):
    stage: str = Field(min_length=2, max_length=100)
    title: str = Field(min_length=2, max_length=240)
    required: bool = True
    notes: str | None = Field(default=None, max_length=2000)


class StageChecklistUpdate(BaseModel):
    status: str
    notes: str | None = Field(default=None, max_length=2000)


def _stage_role(stage: str) -> str | None:
    return {
        "Application Intake": "scrutiny_officer", "Scrutiny": "scrutiny_officer", "Re-scrutiny": "scrutiny_officer",
        "EVC Scheduling": "evc_officer", "EVC Visit": "evc_officer",
        "SAC/SHC Review": "sac_shc_officer", "Compliance": "compliance_officer",
    }.get(stage)


@router.get("/api/v1/applications/{application_id}/stage-checklist")
def get_stage_checklist(application_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if user.role not in {"admin", "officer", "reviewer", "auditor", "scrutiny_officer", "evc_officer", "sac_shc_officer", "compliance_officer", "final_approval_officer"}:
        raise HTTPException(403, "Authorised staff role required")
    if not db.get(Application, application_id):
        raise HTTPException(404, "Application not found")
    rows = db.scalars(select(StageChecklistItem).where(StageChecklistItem.application_id == application_id).order_by(StageChecklistItem.stage, StageChecklistItem.id)).all()
    return [{"id": row.id, "application_id": row.application_id, "stage": row.stage, "title": row.title,
             "required": row.required, "status": row.status, "notes": row.notes,
             "updated_by": row.updated_by, "updated_at": row.updated_at} for row in rows]


@router.post("/api/v1/applications/{application_id}/stage-checklist", status_code=201)
def create_stage_checklist_item(application_id: int, payload: StageChecklistCreate, db: Session = Depends(get_db), user: User = Depends(current_user)):
    application = db.get(Application, application_id)
    if not application:
        raise HTTPException(404, "Application not found")
    if user.role not in {"admin", "officer"} and user.role != _stage_role(payload.stage):
        raise HTTPException(403, "Only an authorised officer for this stage may add checklist items")
    item = StageChecklistItem(application_id=application_id, stage=payload.stage, title=payload.title.strip(),
                              required=payload.required, status="Pending", notes=payload.notes, updated_by=user.full_name)
    db.add(item)
    db.flush()
    _audit(db, "STAGE_CHECKLIST_ITEM_CREATED", user.full_name, application_id, f"{payload.stage}: {payload.title}")
    db.commit()
    db.refresh(item)
    return {"id": item.id, "application_id": item.application_id, "stage": item.stage, "title": item.title,
            "required": item.required, "status": item.status, "notes": item.notes, "updated_by": item.updated_by}


@router.patch("/api/v1/stage-checklist/{item_id}")
def update_stage_checklist_item(item_id: int, payload: StageChecklistUpdate, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = db.get(StageChecklistItem, item_id)
    if not item:
        raise HTTPException(404, "Stage checklist item not found")
    if user.role not in {"admin", "reviewer"} and user.role != _stage_role(item.stage):
        raise HTTPException(403, "Your role cannot update this stage checklist")
    if payload.status not in {"Pending", "Verified", "Blocked", "Not Applicable"}:
        raise HTTPException(422, "Status must be Pending, Verified, Blocked or Not Applicable")
    if payload.status == "Blocked" and not (payload.notes or item.notes or "").strip():
        raise HTTPException(422, "Add a note explaining why this checklist item is blocked")
    item.status = payload.status
    if payload.notes is not None:
        item.notes = payload.notes
    item.updated_by = user.full_name
    item.updated_at = datetime.now(timezone.utc)
    _audit(db, "STAGE_CHECKLIST_ITEM_UPDATED", user.full_name, item.application_id, f"{item.stage}: {item.title} -> {item.status}")
    db.commit()
    return {"id": item.id, "status": item.status, "updated_by": item.updated_by, "updated_at": item.updated_at}


@router.post("/api/v1/college/documents/{document_id}/replace")
async def replace_college_document(document_id: int, file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(current_user)):
    if user.role != "college":
        raise HTTPException(403, "College account required")
    profile = db.scalar(select(CollegeProfile).where(CollegeProfile.user_id == user.id))
    if not profile:
        raise HTTPException(404, "College profile not found")
    record = db.get(DocumentRecord, document_id)
    if not record or record.application_id != profile.application_id:
        raise HTTPException(404, "Document not found for this college application")
    safe_name = Path(file.filename or "").name
    if not safe_name or Path(safe_name).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise HTTPException(415, "Upload a PDF, PNG or JPEG file")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise HTTPException(422, "File is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File exceeds the {settings.max_upload_mb} MB limit")
    old_path = Path(settings.upload_dir) / record.storage_key
    storage_key = uuid4().hex + Path(safe_name).suffix.lower()
    target = Path(settings.upload_dir) / storage_key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    record.storage_key = storage_key
    record.original_name = safe_name
    record.content_type = file.content_type or "application/octet-stream"
    record.size_bytes = len(data)
    record.uploaded_by = profile.institution_name
    check = db.scalar(select(DocumentQualityCheck).where(DocumentQualityCheck.document_id == record.id))
    findings = _analyse_document(check.slot_name if check else "College document", safe_name, data, profile.institution_name)
    if check:
        check.status = "Needs Correction" if findings else ("Manual Review Required" if Path(safe_name).suffix.lower() in {".png", ".jpg", ".jpeg"} else "Preliminary Checks Passed")
        check.findings_json = json.dumps(findings)
        check.checked_at = datetime.now(timezone.utc)
    remaining = db.scalars(select(DocumentQualityCheck).where(
        DocumentQualityCheck.application_id == profile.application_id,
        DocumentQualityCheck.status == "Needs Correction",
        DocumentQualityCheck.document_id != record.id,
    )).all()
    all_issues = [f"{row.slot_name}: {finding}" for row in remaining for finding in json.loads(row.findings_json)]
    all_issues.extend([f"{check.slot_name}: {finding}" for finding in findings] if check else findings)
    _audit(db, "COLLEGE_DOCUMENT_REPLACED", profile.institution_name, profile.application_id, f"Corrected document: {check.slot_name if check else safe_name}")
    if all_issues:
        alert = Alert(application_id=profile.application_id, title="Document correction required",
                      message=f"{len(all_issues)} preliminary document issue(s) remain; checking email delivery.",
                      severity="High", category="Document Quality")
        db.add(alert)
        try:
            email_sent = _send_correction_email(profile.contact_email, db.get(Application, profile.application_id).application_no,
                                                profile.institution_name, all_issues)
            if email_sent:
                _audit(db, "CORRECTION_EMAIL_SENT", "system", profile.application_id, f"Correction notice sent to {profile.contact_email}")
                alert.message = f"{len(all_issues)} preliminary document issue(s) remain. A correction email was sent."
            else:
                alert.message = f"{len(all_issues)} preliminary document issue(s) remain. Email delivery is not configured; follow up through an approved channel."
        except Exception as exc:
            alert.message = f"{len(all_issues)} preliminary document issue(s) remain. Correction email delivery failed."
            _audit(db, "CORRECTION_EMAIL_FAILED", "system", profile.application_id, f"SMTP delivery failed: {type(exc).__name__}")
    else:
        for alert in db.scalars(select(Alert).where(Alert.application_id == profile.application_id, Alert.category == "Document Quality", Alert.acknowledged.is_(False))).all():
            alert.acknowledged = True
            alert.acknowledged_by = "system"
        _audit(db, "DOCUMENT_CORRECTIONS_CLEARED", "system", profile.application_id, "No remaining automated correction findings; human verification is still required.")
    db.commit()
    if old_path.exists():
        try:
            old_path.unlink()
        except OSError:
            pass
    return {"document_id": record.id, "status": check.status if check else "Manual Review Required",
            "findings": findings, "remaining_issues": all_issues}
