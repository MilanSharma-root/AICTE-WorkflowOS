from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.config import settings
from app.database import Base, engine, get_db
from app.models import Application, AuditEvent
from app.schemas import ApplicationCreate, ApplicationOut

@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield

app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"status": "ok", "service": settings.app_name}

@app.get("/api/v1/applications", response_model=list[ApplicationOut])
def list_applications(db: Session = Depends(get_db)):
    return list(db.scalars(select(Application).order_by(Application.submitted_at.desc())).all())

@app.post("/api/v1/applications", response_model=ApplicationOut, status_code=201)
def create_application(payload: ApplicationCreate, db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    application = Application(
        application_no=f"AICTE-{now:%Y}-{now:%m%d%H%M%S}",
        institution_name=payload.institution_name,
        institution_type=payload.institution_type,
        state=payload.state,
        application_type=payload.application_type,
        current_stage=payload.current_stage,
        priority=payload.priority,
        assigned_to=payload.assigned_to,
        due_date=payload.due_date,
        notes=payload.notes,
        status="Pending",
    )
    db.add(application)
    db.flush()
    db.add(AuditEvent(application_id=application.id, action="APPLICATION_CREATED", actor="prototype-user", details=f"Created {application.application_no}"))
    db.commit()
    db.refresh(application)
    return application

@app.get("/api/v1/dashboard/summary")
def dashboard_summary(db: Session = Depends(get_db)):
    total = db.scalar(select(func.count()).select_from(Application)) or 0
    pending = db.scalar(select(func.count()).select_from(Application).where(Application.status == "Pending")) or 0
    stage_rows = db.execute(select(Application.current_stage, func.count()).group_by(Application.current_stage)).all()
    return {
        "total_applications": total,
        "pending_applications": pending,
        "overdue_applications": db.scalar(select(func.count()).select_from(Application).where(Application.due_date < datetime.now(timezone.utc), Application.status != "Completed")) or 0,
        "stage_counts": [{"stage": stage, "count": count} for stage, count in stage_rows],
    }
