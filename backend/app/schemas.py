from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field
class LoginIn(BaseModel):
    username: str
    password: str
class ApplicationCreate(BaseModel):
    institution_name: str = Field(min_length=2, max_length=240)
    institution_type: str = Field(default="Technical Institution", max_length=80)
    state: str = Field(default="Uttar Pradesh", max_length=100)
    application_type: str = Field(default="Extension of Approval", max_length=120)
    current_stage: str = Field(default="Application Intake", max_length=100)
    priority: str = Field(default="Normal", max_length=30)
    assigned_to: str | None = Field(default=None, max_length=160)
    due_date: datetime | None = None
    notes: str | None = None
class ApplicationUpdate(BaseModel):
    institution_name: str | None = Field(default=None, min_length=2, max_length=240)
    institution_type: str | None = None
    state: str | None = None
    application_type: str | None = None
    priority: str | None = None
    assigned_to: str | None = None
    due_date: datetime | None = None
    notes: str | None = None
class TransitionIn(BaseModel):
    to_stage: str
    status: str = "In Review"
    note: str | None = None
class ComplianceCreate(BaseModel):
    application_id: int
    title: str = Field(min_length=2, max_length=240)
    category: str = "Document validity"
    due_date: datetime | None = None
    assigned_to: str | None = None
    notes: str | None = None
class ComplianceUpdate(BaseModel):
    status: str | None = None
    due_date: datetime | None = None
    assigned_to: str | None = None
    notes: str | None = None
class EVCVisitCreate(BaseModel):
    application_id: int
    proposed_date: datetime | None = None
    location: str = "Institution campus"
    panel_members: str | None = None
class EVCVisitUpdate(BaseModel):
    proposed_date: datetime | None = None
    location: str | None = None
    panel_members: str | None = None
    status: str | None = None
    outcome: str | None = None
class UndertakingCreate(BaseModel):
    application_id: int
    title: str = Field(min_length=2, max_length=240)
    due_date: datetime | None = None
    notes: str | None = None
class UndertakingUpdate(BaseModel):
    submitted: bool | None = None
    due_date: datetime | None = None
    notes: str | None = None
class ApplicationOut(BaseModel):
    id: int
    application_no: str
    institution_name: str
    institution_type: str
    state: str
    application_type: str
    current_stage: str
    status: str
    priority: str
    assigned_to: str | None
    submitted_at: datetime
    due_date: datetime | None
    notes: str | None
    model_config = ConfigDict(from_attributes=True)

class WorkflowConfigUpdate(BaseModel):
    name: str = Field(default="Custom approval route", min_length=2, max_length=160)
    stages: list[str] = Field(min_length=2, max_length=30)

class ApplicationWorkflowRouteUpdate(BaseModel):
    application_type: str = Field(min_length=2, max_length=120)
    stages: list[str] = Field(min_length=2, max_length=30)

class DocumentChecklistTemplateUpdate(BaseModel):
    application_type: str = Field(min_length=2, max_length=120)
    items: list[dict] = Field(min_length=1, max_length=100)

class DocumentCheckUpdate(BaseModel):
    status: str
    document_id: int | None = None
    notes: str | None = Field(default=None, max_length=2000)


class DocumentValidityCreate(BaseModel):
    application_id: int
    document_name: str = Field(min_length=2, max_length=240)
    category: str = Field(default="Lease / ownership", max_length=100)
    document_id: int | None = None
    issued_on: datetime | None = None
    expiry_date: datetime | None = None
    renewal_due_date: datetime | None = None
    notes: str | None = Field(default=None, max_length=2000)

class DocumentValidityUpdate(BaseModel):
    document_name: str | None = Field(default=None, min_length=2, max_length=240)
    category: str | None = Field(default=None, max_length=100)
    document_id: int | None = None
    issued_on: datetime | None = None
    expiry_date: datetime | None = None
    renewal_due_date: datetime | None = None
    status: str | None = None
    notes: str | None = Field(default=None, max_length=2000)
