# AICTE WorkflowOS

A starter platform for AICTE approval workflow tracking, application intake, dashboard summaries, and audit-ready workflow foundations.

## Current prototype

- React + TypeScript + Vite dashboard
- FastAPI backend
- SQLAlchemy models for applications and audit events
- PostgreSQL configuration through environment variables
- Windows `start.bat` launcher for local development without Docker
- Synthetic demo records for UI exploration

## Run on Windows

1. Install Python 3.11+ and Node.js LTS.
2. Configure `backend/.env` using `.env.example` and make sure PostgreSQL is running.
3. Run `start.bat` from this project directory.
4. Open the frontend at `http://localhost:5173`.

## Important

This is an early prototype. Configure and validate approval pathways, statutory rules, role permissions, deadlines, and notifications against the applicable official AICTE handbook and institutional requirements before real use. Demo records are synthetic; the prototype does not automate or replace statutory scrutiny or decisions by competent authorities.
