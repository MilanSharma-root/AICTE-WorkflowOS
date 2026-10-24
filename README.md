# AICTE WorkflowOS

AICTE Approval Workflow Automation, Compliance Monitoring and Alert Management prototype. This is a local development prototype, not an official AICTE service.

## Implemented modules

- React + TypeScript + Vite operations dashboard connected to FastAPI APIs.
- Live dashboard metrics include application/workflow counts plus document records expiring within 30 days and already expired records, calculated from the backend database.
- Application register with create/search/filter, case assignments, due dates and database persistence.
- Admin-configurable default workflow stages plus application-type-specific routes, sequential stage progression, return → re-scrutiny loops, return/rejection reasons and per-application workflow history. Invalid stage skips and inconsistent terminal status combinations are rejected.
- EVC scheduling, panel notes, status and outcome fields.
- Compliance task register for undertakings, faculty/infrastructure and other requirements, plus a dedicated document-validity lifecycle API for lease/ownership and other dated records (issue date, expiry, renewal due date, linked uploaded evidence, status and audit history).
- Time-bound undertakings with due dates and submission tracking.
- Deadline alert generation for applications, compliance tasks, undertakings, EVC visits and expiring/expired document-validity records, with severity, acknowledgement and escalation-oriented queues; an in-process scheduler checks application, compliance, undertaking and upcoming EVC due dates every 15 minutes while the API is running.
- Rule-based automation runner (`POST /api/v1/automation/run`) that scans open cases, routes unassigned cases to the authorised operator running it, flags missing document records, unresolved required checklist items and missing internal target dates, refreshes deadline alerts, and writes audit events. `GET /api/v1/automation/status` exposes live workload signals displayed on the overview dashboard.
- Tiered overdue follow-up alerts (L1 after 1 day, L2 after 3 days, L3 after 7 days) with level-specific deduplication; `GET /api/v1/automation/escalation-policy` describes the illustrative thresholds. Actual recipient hierarchy and email/SMS delivery are not configured.
- Local document upload/download API with extension allowlist, size cap and opaque storage names; files are not exposed as static public assets. Document validity records can link to evidence uploaded for the same application.
- Configurable, admin-managed document checklist templates by application type; apply templates to applications, link uploaded file records to checklist items, track Missing/Uploaded/Verified/Rejected/Not Applicable statuses, and generate a pre-scrutiny summary for human review. Templates are not official until validated by the competent authority.
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
- `scrutiny@demo.local` — Scrutiny and re-scrutiny queue.
- `evc@demo.local` — EVC queue and visit status updates.
- `sac.shc@demo.local` — SAC/SHC review queue.
- `compliance@demo.local` — Compliance queue and checklists.
- `final.approval@demo.local` — final approval queue (authorised closure only).

These are public demo credentials. Do not use them for a deployed or real-data environment. Set a long random `SECRET_KEY`, create proper users/passwords, configure HTTPS, backups, retention, access controls and secure storage before any deployment.

## Configuration

The backend reads `backend/.env` when launched from the backend directory. By default, no .env file is required; it uses `sqlite:///./aicte_workflowos.db`. To use PostgreSQL, set a valid SQLAlchemy URL, e.g. `postgresql+psycopg://USER:PASSWORD@HOST:5432/aicte_workflowos`.

For production, set `APP_ENV=production`, a cryptographically random `SECRET_KEY` of at least 32 characters, explicit HTTPS frontend origins in `CORS_ORIGINS`, and a durable `DATABASE_URL`. Production startup rejects the demo/default secret, weak secrets, and wildcard CORS. Localhost frontend origins are added automatically only outside production. Keep the public demo credentials disabled/changed before any deployment.

Uploaded files are stored under `backend/storage/documents` by default and require authenticated API access to download. Set upload limits and production storage controls appropriate to your environment.

## Important limitations

- Fresh databases seed demo login accounts only; application records are created from actual portal submissions or authorised staff entry. Existing local databases may still contain records created by earlier prototype versions.
- Workflow stages are illustrative and configurable, but must be validated against the applicable AICTE Approval Process Handbook (APH), official circulars, jurisdiction and competent-authority rules.
- The risk score is an explainable prioritisation aid, not an AICTE decision, compliance finding, or substitute for expert review.
- SMTP correction emails are sent only when SMTP_HOST and SMTP_FROM are configured; SMTP_USERNAME, SMTP_PASSWORD, SMTP_PORT, SMTP_USE_TLS and SMTP_USE_SSL control transport/authentication. SMS delivery, eOffice/ERP integration, Aadhaar/digital-signature integration, production-grade identity federation, antivirus scanning, encrypted object storage and deployment hardening are not enabled. Image checks use resolution/blur heuristics without OCR; PDF checks inspect extractable text and simple institution/date/reference signals. These checks can miss errors and must never be treated as authenticity verification or official compliance findings.
- Statutory approvals, rejections, EVC outcomes and committee decisions remain under authorised human control. Do not load sensitive personal or institutional documents until security and retention controls are reviewed.

## Document validity API

- `GET /api/v1/document-validity?application_id=<id>` lists validity records, optionally scoped to an application.
- `POST /api/v1/document-validity` creates a record with optional linked uploaded evidence, issue date, expiry date and renewal due date.
- `PATCH /api/v1/document-validity/{id}` updates status/metadata and records an audit event. Supported statuses: Active, Renewal Due, Under Verification, Expired, Renewed, Not Applicable.
- Expiry monitoring creates in-app alerts within 30 days, with higher severity within 7 days and for expired records. Alert creation is not a legal determination; staff must verify the record and applicable requirements.
- These APIs extend the prototype database schema; `create_all` creates the new table for fresh/local databases. Existing installations should back up the database before updating.


## College intake and document checks

- The College option creates an institutional account and application together. All three required upload slots must be supplied: Institution Registration / Approval Proof, Land / Building Evidence, and Faculty / Infrastructure Details. Accepted formats are PDF, PNG and JPEG; the existing configured upload-size limit applies to each file.
- Colleges can sign in with the email/password created during submission, view only their linked application, and replace a flagged document. Application and document metadata are stored in the configured shared database; file bytes are stored in the configured upload directory, so multi-device use requires a shared/durable database and shared/durable file storage.
- Automated checks flag empty/invalid files, low-resolution or blurred images, PDFs with little/no extractable text, and simple missing institution/date/reference signals where text can be extracted. Image text is not OCR-verified. Manual review remains required.
- Correction email delivery is conditional on SMTP environment variables. If SMTP is not configured or delivery fails, the application receives an in-app alert and the audit trail records the delivery outcome; the system does not claim an email was sent.
- A stage officer can choose Stage Completed while the current stage is selected. The backend routes to the next configured stage only if that role is authorised and required checklist items / document correction blockers are clear. Final approval requires the final-approval role (or an existing admin/reviewer account).
- New tables (college_profiles, document_quality_checks, stage_checklist_items) are created by the existing SQLAlchemy create_all startup path. No existing application/user columns were changed. Back up the database before upgrading an existing installation.
## APH workflow safeguards

The prototype's workflow configuration is an administrative configuration aid, not a substitute for a legally approved APH rule engine. The 2024–2027 APH describes different processing paths by application category (see Table 2.3 and the relevant clauses), so an operator must validate the selected route and deadlines with authorised AICTE officials before operational use.

- Workflow transitions reject unknown stages and closed applications cannot be silently transitioned again.
- Application-type-specific routes can be configured by admins and are enforced when reviewers transition an application; routes use canonical prototype stages and preserve terminal outcomes.
- Return/rejection actions require a recorded reason. Normal progression is one configured stage at a time; returned cases resume at re-scrutiny (or the configured scrutiny fallback), and closure requires the Completed status at Approved / Closed.
- Every successful transition writes a workflow event and an audit event.
- Approval/closure remains an authorised human decision; automated reminders and risk indicators are decision support only.
- Deadline alerts are generated at API startup and checked every 15 minutes while the API process is running, with an additional refresh when the alert endpoint is read. The manual automation runner also assigns unowned cases to the authorised operator running it, flags missing document records and missing internal target dates, and records an audit event. This in-process scheduler is suitable only for a local prototype; production should use a durable scheduled worker and configure notification delivery/escalation channels.
- EVC, undertaking, compliance and document records are prototype records. Do not use this demo with real institutional or personal data.

## Verification

GitHub Actions CI runs the backend test suite and the frontend TypeScript/Vite production build on pushes to `main` / `ui/premium-government-dashboard` and on pull requests targeting `main`.

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


## SMTP email configuration

The college correction-email flow uses environment variables in the backend runtime. SMTP credentials must never be committed to Git.

Set these variables in backend/.env for local development (create the file yourself; do not commit it), or in your deployment provider's backend service environment settings:

```dotenv
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-sender@gmail.com
SMTP_PASSWORD=your-16-character-google-app-password
SMTP_FROM=your-sender@gmail.com
SMTP_USE_TLS=true
SMTP_USE_SSL=false
```

- For Gmail, enable 2-Step Verification and create an App Password in your Google Account. Use that app password as SMTP_PASSWORD; do not use your normal Google password.
- Port 587 uses STARTTLS (SMTP_USE_TLS=true, SMTP_USE_SSL=false). For providers requiring implicit TLS, commonly port 465, use SMTP_USE_SSL=true and SMTP_USE_TLS=false.
- SMTP_FROM should be an address permitted by the SMTP provider. The college's registered email address is the recipient.
- Restart the backend after changing environment variables. In a hosted deployment, add the variables to the backend service's environment settings and redeploy/restart that service.
- If SMTP_HOST or SMTP_FROM is missing, the application records that email was not sent. Failed SMTP authentication or delivery should be checked in backend logs/audit events.
- Send a real test only to an address you control before demoing with college submissions. Never commit .env, app passwords, or SMTP secrets to Git.
