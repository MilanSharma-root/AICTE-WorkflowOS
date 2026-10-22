from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field

class ApplicationCreate(BaseModel):
    institution_name: str = Field(min_length=2, max_length=240)
    institution_type: str = Field(default="Technical Institution", max_length=80)
    state: str = Field(default="Uttar Pradesh", max_length=100)
    application_type: str = Field(default="Extension of Approval", max_length=120)
    current_stage: str = Field(default="Scrutiny", max_length=100)
    priority: str = Field(default="Normal", max_length=30)
    assigned_to: str | None = Field(default=None, max_length=160)
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
