# Next.js Incident Command UI

Mobile-first PWA frontend for the AI Operations Command Center.

## Run

```bash
cd web
npm install
npm run dev
```

Open `http://localhost:3000`.

By default the UI uses same-origin API requests, which is what the integrated
Railway deployment expects.

For standalone frontend development, point it at the FastAPI backend:

```bash
NEXT_PUBLIC_API_BASE=http://localhost:8000 npm run dev
```

## Build

```bash
npm run typecheck
npm run build
```

## Local Ollama and online fallback

The Jarvis frontend supports a local Ollama runtime plus an online fallback.
The Admin page can select the local model and check whether the host can run it.
If the configured local model is not available or not feasible, the UI prompts
for an online provider API key and uses the next available runtime gracefully.

## Low disk space note

This workspace lives on `C:`. If `npm install` fails with `ENOSPC`, copy the
`web/` folder to a roomier drive such as `D:`, install there, and keep
`NEXT_PUBLIC_API_BASE` pointed at the FastAPI backend.
