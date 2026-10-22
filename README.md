# AICTE WorkflowOS

AICTE Approval Workflow Automation, Compliance Monitoring and Alert Management prototype. This is a local development prototype, not an official AICTE service.

## Implemented modules

- React + TypeScript + Vite operations dashboard connected to FastAPI APIs.
- Application register with create/search/filter, case assignments, due dates and database persistence.
- Admin-configurable default workflow stages plus application-type-specific routes, stage transitions, return/review notes and per-application workflow history.
- EVC scheduling, panel notes, status and outcome fields.
- Compliance task register for document validity, undertakings, faculty/infrastructure and other requirements.
- Time-bound undertakings with due dates and submission tracking.
- Deadline alert generation, severity, acknowledgement and escalation-oriented queues; an in-process scheduler checks application, compliance, undertaking and upcoming EVC due dates every 15 minutes while the API is running.
- Local document upload/download API with extension allowlist, size cap and opaque storage names; files are not exposed as static public assets.
- Role-based sign-in and permissions (admin, officer, reviewer, auditor), signed expiring bearer tokens, hashed demo passwords.
- Audit events, explainable triage risk score, stage/state summary and CSV export.
- SQLite-first local run; PostgreSQL remains supported through DATABASE_URL.

## Windows setup (Docker not required)

1. Install Python 3.11+ and Node.js LTS.
2. Open PowerShell in this project folder.
3. Run `.\start.bat` (or double-click `start.bat` from File Explorer).
4. The launcher installs requirements, then opens the API and frontend terminals.
5. Open http://localhost:5173 and sign in with a demo account below.
6. API documentation: http://127.0.0.1:8000/docs

If you already have the project running, stop its backend terminal with Ctrl+C, pull the latest repository changes, then run `start.bat` again so new dependencies and code are loaded.

## Demo accounts

All prototype accounts use `Demo@12345`:

- `admin@demo.local` — configure workflow stages, review audit trail, manage records.
- `officer@demo.local` — register applications, schedule EVC, track compliance and undertakings.
- `reviewer@demo.local` — record workflow transitions and compliance review.
- `auditor@demo.local` — read-only access and audit trail.

These are public demo credentials. Do not use them for a deployed or real-data environment. Set a long random `SECRET_KEY`, create proper users/passwords, configure HTTPS, backups, retention, access controls and secure storage before any deployment.

## Configuration

The backend reads `backend/.env` when launched from the backend directory. By default, no .env file is required; it uses `sqlite:///./aicte_workflowos.db`. To use PostgreSQL, set a valid SQLAlchemy URL, e.g. `postgresql+psycopg://USER:PASSWORD@HOST:5432/aicte_workflowos`.

Uploaded files are stored under `backend/storage/documents` by default and require authenticated API access to download. Set upload limits and production storage controls appropriate to your environment.

## Important limitations

- Seeded records are synthetic demonstration data, not real institutional applications.
- Workflow stages are illustrative and configurable, but must be validated against the applicable AICTE Approval Process Handbook (APH), official circulars, jurisdiction and competent-authority rules.
- The risk score is an explainable prioritisation aid, not an AICTE decision, compliance finding, or substitute for expert review.
- Email/SMS delivery, eOffice/ERP integration, Aadhaar/digital-signature integration, production-grade identity federation, antivirus scanning, encrypted object storage, migrations and deployment hardening are not enabled by this local prototype.
- Statutory approvals, rejections, EVC outcomes and committee decisions remain under authorised human control. Do not load sensitive personal or institutional documents until security and retention controls are reviewed.

## APH workflow safeguards

The prototype's workflow configuration is an administrative configuration aid, not a substitute for a legally approved APH rule engine. The 2024–2027 APH describes different processing paths by application category (see Table 2.3 and the relevant clauses), so an operator must validate the selected route and deadlines with authorised AICTE officials before operational use.

- Workflow transitions reject unknown stages and closed applications cannot be silently transitioned again.
- Application-type-specific routes can be configured by admins and are enforced when reviewers transition an application; routes use canonical prototype stages and preserve terminal outcomes.
- Return/rejection actions require a recorded reason.
- Every successful transition writes a workflow event and an audit event.
- Approval/closure remains an authorised human decision; automated reminders and risk indicators are decision support only.
- Deadline alerts are generated at API startup and checked every 15 minutes while the API process is running, with an additional refresh when the alert endpoint is read. This in-process scheduler is suitable only for a local prototype; production should use a durable scheduled worker and configure notification delivery/escalation channels.
- EVC, undertaking, compliance and document records are prototype records. Do not use this demo with real institutional or personal data.

## Verification

From `backend`, activate the project virtual environment and run:

```powershell
python -m pytest -q
```

From `frontend`, run:

```powershell
npm install
npm run build
```

A passing local build/test run is still required after pulling changes. This repository does not claim certification, regulatory approval, or integration with official AICTE systems.
