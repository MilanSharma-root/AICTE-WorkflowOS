from datetime import datetime, timezone
from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base

def utcnow():
    return datetime.now(timezone.utc)

class Application(Base):
    __tablename__ = "applications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    application_no: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    institution_name: Mapped[str] = mapped_column(String(240), index=True)
    institution_type: Mapped[str] = mapped_column(String(80), default="Technical Institution")
    state: Mapped[str] = mapped_column(String(100), default="Uttar Pradesh")
    application_type: Mapped[str] = mapped_column(String(120), default="Extension of Approval")
    current_stage: Mapped[str] = mapped_column(String(100), default="Scrutiny")
    status: Mapped[str] = mapped_column(String(60), default="Pending")
    priority: Mapped[str] = mapped_column(String(30), default="Normal")
    assigned_to: Mapped[str | None] = mapped_column(String(160), nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    application_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(120))
    actor: Mapped[str] = mapped_column(String(160), default="system")
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
