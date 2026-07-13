---
name: incident-response-startup-validation
description: Validate the incident response system startup flow, local port handling, and frontend/backend integration for this repository.
license: MIT
---

## Overview

This skill validates the local incident response repository's startup flow. It is intended for the current project and focuses on the `run.ps1` launcher, the frontend/backend integration, and the runtime health checks.

## Purpose

Use this skill to:

- verify `run.ps1` selects free backend and frontend ports
- verify `run.bat` and `run-local.bat` wrap `run.ps1` correctly
- validate `NEXT_PUBLIC_API_BASE` is set for the frontend to reach the backend
- confirm the backend health endpoint responds
- detect and resolve port conflicts for the local developer workflow

## Validation Process

### 1. Confirm repository startup expectations

- Backend: `venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port <ApiPort>`
- Frontend: `npm run dev -- --hostname 127.0.0.1 --port <WebPort>` from `/web`
- `run.ps1` should use defaults `8000` and `3000` if available and otherwise find free ports.
- `run-local.bat` should execute `run.ps1` with explicit local ports for development.

### 2. Validate the skill metadata

- Confirm YAML frontmatter contains `name`, `description`, and `license`.
- Confirm the body is aligned with the current repository and not a generic architecture essay.
- Confirm the file documents the actual startup and validation workflow.

### 3. Check port availability

- Validate `8000` and `3000` are available or detect which processes own them.
- If occupied, search for free ports between `8000` and `8100` for backend and `3000` and `3100` for frontend.
- Prefer `8111` and `3111` only when default ports are unavailable.

### 4. Run the startup flow for validation

1. Launch the project from the repo root using:

```powershell
cd d:\saarthi_models\incident-response-system
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 -ApiPort 8111 -WebPort 3111 -Dev -SkipInstall
```

2. Confirm the PowerShell output includes:

- `Starting backend on http://localhost:8111`
- `Setting NEXT_PUBLIC_API_BASE for frontend to http://127.0.0.1:8111`
- `Starting frontend on http://localhost:3111`
- `AI Operations Command Center is running.`

3. Confirm backend health responds at `http://127.0.0.1:8111/api/health`.
4. Confirm the frontend loads in a browser at `http://localhost:3111`.

### 5. Validate wrappers

- `run.bat` must forward arguments exactly to `run.ps1`.
- `run-local.bat` must invoke `run.ps1` with `-ApiPort 8000 -WebPort 3000`.

## Troubleshooting

- If `npm run dev` fails with `EADDRINUSE`, the chosen frontend port is still occupied.
- If `uvicorn` fails to bind, the backend port is occupied or the Python venv is misconfigured.
- If the frontend starts but cannot reach the backend, verify `NEXT_PUBLIC_API_BASE` is set in the launching PowerShell session.
- If the backend health endpoint fails, inspect backend logs and confirm `app:app` is loading correctly.

## Verification Checklist

- [ ] `SKILL.md` metadata is valid and repo-specific.
- [ ] The skill file describes the actual startup flow.
- [ ] `run.ps1` can start backend and frontend on free ports.
- [ ] `run.bat` and `run-local.bat` are simple wrappers.
- [ ] `NEXT_PUBLIC_API_BASE` is set to the running backend URL.
- [ ] `GET /api/health` responds successfully.
- [ ] The frontend loads at the selected local URL.

## Expected runtime output

When validation succeeds, the following lines should appear:

- `Starting backend on http://localhost:<ApiPort>`
- `Setting NEXT_PUBLIC_API_BASE for frontend to http://127.0.0.1:<ApiPort>`
- `Starting frontend on http://localhost:<WebPort>`
- `AI Operations Command Center is running.`

## Notes

This skill is intentionally narrow: it validates the local incident response system startup and external integration points, not general agent architecture.
