# AIOC Deployment Guide

This guide covers production deployment for the AIOC live demo, including the combined backend + frontend container, required environment variables, and Railway configuration.

## Deployment architecture

AIOC runs as a single Docker container that hosts:

- FastAPI backend on `PORT` (default `8000`)
- Next.js frontend on `WEB_PORT` (default `3000`)
- `start.sh` launches the frontend first, waits until it is ready, then starts the backend
- The backend proxies web routes such as `/`, `/admin`, and `/incident/{incident_id}` to the local Next.js server

The container exposes only the backend port to the outside world. The frontend remains internal and is reachable via `WEB_INTERNAL_URL` from the backend process.

## Production environment variables

### Required

- `OPENAI_API_KEY`
  - Primary online LLM for production reasoning.
  - If omitted, AIOC will fall back to the heuristic answer path.
- `SERVICE`
  - The service name used by the default incident scenarios and dashboard context.
- `PUBLIC_BASE_URL`
  - Public URL for the deployed app (for generated incident links and messages).
- `WEB_BASE_URL`
  - Usually the same as `PUBLIC_BASE_URL` unless the frontend is served from a separate URL.
- `WEB_INTERNAL_URL`
  - Internal URL used by the backend to reach the Next.js server. In the default container setup, set to `http://127.0.0.1:3000`.
- `API_AUTH_TOKEN`
  - Token used to protect API endpoints in production.

### Runtime ports

- `PORT=8000`
  - Backend port exposed by the container.
- `WEB_PORT=3000`
  - Internal frontend port used by the Next.js server.

### Optional online provider fallback

- `LLM_PROVIDER`
  - Set to one of `openai`, `gemini`, `groq`, or `claude`.
- `GEMINI_API_KEY`
- `GEMINI_MODEL`
- `GROQ_API_KEY`
- `GROQ_MODEL`
- `ANTHROPIC_API_KEY`
- `ANTHROPIC_MODEL`

### Optional local Ollama configuration

- `OLLAMA_ENABLED=true`
- `OLLAMA_BASE_URL=http://127.0.0.1:11434`
- `OLLAMA_MODEL=qwen2.5:7b`
- `OLLAMA_ANSWER_CONFIDENCE=0.68`
- `OLLAMA_TIMEOUT_SECONDS=45`

### Optional memory and knowledge configuration

- `QUERY_CACHE_TTL_SECONDS=86400`
- `QUERY_CACHE_MIN_CONFIDENCE=0.72`
- `QUERY_CACHE_SIMILARITY_THRESHOLD=0.90`
- `QUERY_CACHE_VERSION=graph-query-v2`
- `GRAPH_MEMORY_BACKEND=memgraph`
- `MEMGRAPH_URI=bolt://127.0.0.1:7687`
- `MEMGRAPH_USERNAME=`
- `MEMGRAPH_PASSWORD=`

### Optional alerting and collaboration

- `WAR_ROOM_WEBHOOK_URL`
- `TEAMS_ALERT_WEBHOOK_URL`
- `TEAMS_PROJECT_OWNER`
- `TEAMS_ADMIN`
- `EMAIL_SMTP_HOST`
- `EMAIL_SMTP_PORT`
- `EMAIL_SMTP_USERNAME`
- `EMAIL_SMTP_PASSWORD`
- `EMAIL_USE_TLS=true`
- `EMAIL_FROM`
- `EMAIL_FROM_DOMAIN`
- `HIGH_RISK_ALERT_EMAIL_TO`
- `SLACK_SIGNING_SECRET`
- `SLACK_BOT_TOKEN`
- `SLACK_DEFAULT_CHANNEL`
- `SLACK_ALLOW_UNSIGNED_LOCAL=false`

### Optional telemetry

- `TELEMETRY_PROVIDER=mock`
- `PROMETHEUS_URL`
- `PROMETHEUS_QUERY_ERROR_RATE`
- `PROMETHEUS_QUERY_LATENCY`
- `PROMETHEUS_QUERY_CPU`
- `PROMETHEUS_QUERY_MEMORY`
- `LOKI_URL`
- `LOKI_QUERY`
- `ALERTMANAGER_URL`
- `GITLAB_URL`
- `GITLAB_PROJECT_ID`
- `GITLAB_TOKEN`

## Railway configuration

The repository already includes `railway.toml` with the production deployment settings:

- `builder = "DOCKERFILE"`
- `dockerfilePath = "Dockerfile"`
- `startCommand = "sh /app/start.sh"`
- `healthcheckPath = "/api/health"`
- `healthcheckTimeout = 180`
- `restartPolicyType = "ON_FAILURE"`
- `restartPolicyMaxRetries = 10`

### Railway environment

Set these in Railway:

- `PORT=8000`
- `WEB_PORT=3000`
- `PUBLIC_BASE_URL=https://<your-railway-domain>`
- `WEB_BASE_URL=https://<your-railway-domain>`
- `WEB_INTERNAL_URL=http://127.0.0.1:3000`
- `API_AUTH_TOKEN=<secure-value>`
- `OPENAI_API_KEY=<your-key>`
- Optional: `LLM_PROVIDER`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `ANTHROPIC_API_KEY`, etc.

### Why this works

- Railway exposes the backend port `8000` publicly.
- The backend starts the frontend internally on port `3000`.
- The backend proxies web routes to the local frontend, so `/` and `/admin` work from the public domain.

## Local Docker deploy (production-like)

Build the image:

```bash
docker build -t aioc .
```

Run the container:

```bash
docker run --rm -p 8000:8000 \
  -e PORT=8000 \
  -e WEB_PORT=3000 \
  -e PUBLIC_BASE_URL=http://localhost:8000 \
  -e WEB_BASE_URL=http://localhost:8000 \
  -e WEB_INTERNAL_URL=http://127.0.0.1:3000 \
  -e API_AUTH_TOKEN=demo-token \
  -e OPENAI_API_KEY=sk-xxx \
  aioc
```

Then open `http://localhost:8000`.

## Healthcheck

Production should use `/api/health` as the live health endpoint. The container already starts with this path in `railway.toml`.

## Notes

- `WEB_INTERNAL_URL` must be the internal URL where Next.js is running inside the container.
- `PUBLIC_BASE_URL` and `WEB_BASE_URL` should point to the externally visible app URL for email and notification links.
- If `OPENAI_API_KEY` is missing, the app still runs in heuristic fallback mode, but LLM-powered RCA and Jarvis features will be limited.
- `API_AUTH_TOKEN` is strongly recommended in production to protect API access.
