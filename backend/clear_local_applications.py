"""Safely clear all AICTE WorkflowOS applications from a local SQLite database.

Run from the backend directory:
    python clear_local_applications.py

The script creates a timestamped backup of the SQLite database and upload directory
before asking for explicit confirmation. It preserves staff accounts and workflow
configuration, but removes college accounts linked to the deleted applications.
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import engine
from app.models import (
    Alert,
    Application,
    ApplicationDocumentCheck,
    AuditEvent,
    CollegeProfile,
    ComplianceItem,
    DocumentQualityCheck,
    DocumentRecord,
    DocumentValidity,
    EVCVisit,
    StageChecklistItem,
    Undertaking,
    User,
    WorkflowEvent,
)

def sqlite_file_from_url(url: str) -> Path:
    if not url.startswith("sqlite:///"):
        raise RuntimeError(
            "Safety stop: this cleanup script only supports a local SQLite database. "
            "DATABASE_URL is not a SQLite URL, so nothing was deleted."
        )
    raw = unquote(url.removeprefix("sqlite:///"))
    path = Path(raw)
    return path if path.is_absolute() else (Path.cwd() / path).resolve()

def main() -> None:
    if engine.dialect.name != "sqlite":
        raise RuntimeError("Safety stop: expected a local SQLite database; nothing was deleted.")

    db_path = sqlite_file_from_url(settings.database_url)
    if not db_path.exists():
        raise FileNotFoundError(
            f"Database file not found: {db_path}. Check backend/.env and run this script "
            "from the backend directory. Nothing was deleted."
        )

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = Path.cwd() / "backups" / f"before-clear-applications-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    db_backup = backup_dir / db_path.name

    # SQLite's backup API creates a consistent copy even if the DB uses WAL mode.
    source = sqlite3.connect(str(db_path))
    target = sqlite3.connect(str(db_backup))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()

    upload_dir = Path(settings.upload_dir)
    if not upload_dir.is_absolute():
        upload_dir = (Path.cwd() / upload_dir).resolve()
    upload_backup = backup_dir / "uploaded_documents"
    if upload_dir.exists():
        shutil.copytree(upload_dir, upload_backup)

    print("\nLocal AICTE WorkflowOS cleanup")
    print(f"Database: {db_path}")
    print(f"Backup:   {backup_dir.resolve()}")
    print("This removes every application and its linked workflow/document records.")
    print("It also removes college accounts linked to these applications.")
    print("Staff/admin accounts, workflow routes, checklist templates, and global settings are preserved.")

    with Session(engine) as session:
        application_ids = list(session.scalars(select(Application.id)).all())
        if not application_ids:
            print("\nNo applications found. Nothing to delete.")
            return

        print(f"\nApplications to delete: {len(application_ids)}")
        confirm = input('Type DELETE ALL APPLICATIONS to continue: ').strip()
        if confirm != "DELETE ALL APPLICATIONS":
            print("Confirmation did not match. Nothing was deleted.")
            return

        document_rows = session.scalars(
            select(DocumentRecord).where(DocumentRecord.application_id.in_(application_ids))
        ).all()
        document_paths = [upload_dir / row.storage_key for row in document_rows]

        # Remove dependent records first to respect foreign-key constraints.
        session.execute(delete(ApplicationDocumentCheck).where(
            ApplicationDocumentCheck.application_id.in_(application_ids)))
        session.execute(delete(DocumentValidity).where(
            DocumentValidity.application_id.in_(application_ids)))
        session.execute(delete(DocumentQualityCheck).where(
            DocumentQualityCheck.application_id.in_(application_ids)))
        session.execute(delete(ComplianceItem).where(
            ComplianceItem.application_id.in_(application_ids)))
        session.execute(delete(Undertaking).where(
            Undertaking.application_id.in_(application_ids)))
        session.execute(delete(EVCVisit).where(
            EVCVisit.application_id.in_(application_ids)))
        session.execute(delete(StageChecklistItem).where(
            StageChecklistItem.application_id.in_(application_ids)))
        session.execute(delete(WorkflowEvent).where(
            WorkflowEvent.application_id.in_(application_ids)))
        session.execute(delete(Alert).where(
            Alert.application_id.in_(application_ids)))
        session.execute(delete(AuditEvent).where(
            AuditEvent.application_id.in_(application_ids)))

        profiles = session.scalars(select(CollegeProfile).where(
            CollegeProfile.application_id.in_(application_ids))).all()
        college_user_ids = [profile.user_id for profile in profiles]
        session.execute(delete(CollegeProfile).where(
            CollegeProfile.application_id.in_(application_ids)))
        if college_user_ids:
            session.execute(delete(User).where(
                User.id.in_(college_user_ids), User.role == "college"))

        session.execute(delete(DocumentRecord).where(
            DocumentRecord.application_id.in_(application_ids)))
        session.execute(delete(Application).where(Application.id.in_(application_ids)))
        session.commit()

    # Delete uploaded files only after the database transaction commits.
    for path in document_paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            print(f"Warning: could not remove uploaded file {path}: {exc}")

    print(f"\nDone. Deleted {len(application_ids)} applications and their linked records.")
    print(f"Backup retained at: {backup_dir.resolve()}")

if __name__ == "__main__":
    main()
