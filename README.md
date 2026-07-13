# AI Operations Command Center

Autonomous multi-agent incident response system for RCA, impact analysis, troubleshooting, and governed stakeholder communication.

See `SUBMISSION.md` for the project overview, `ARCHITECTURE.md` for the technical diagram, and `WORKFLOW.md` for the end-to-end operating flow.

## What It Does

- Loads logs, metrics, and deployment context for an incident.
- Synthesizes a grounded root cause hypothesis.
- Calculates user impact, revenue impact, and cost impact.
- Produces a troubleshooting plan and stakeholder-specific updates.
- Exposes a multilingual voice-enabled copilot for incident Q&A and RAG lookup.
- Keeps a human review trail for RCA acceptance, rejection, and override.
- Stores resolved incidents for similarity-based learning.

## Workflow Stages

1. Incident intake from the dashboard, API, or Slack.
2. Evidence loading through the incident commander.
3. Log and metrics analysis.
4. RCA synthesis with optional data refresh if confidence is low.
5. Bounded RCA debate with dynamic evidence/operations critics and a judge.
6. Business impact analysis.
7. Stakeholder update generation.
8. Executive summary and recovery recommendations.
9. Human review, postmortem export, and memory capture.
10. Voice/text copilot queries against incident evidence and the knowledge base.

The Flow tab renders the runtime parent/child agent movement and debate ledger.
Set `RCA_DEBATE_MAX_ROUNDS` from `1` to `3` (default `2`) to cap debate cost and latency.

## Run

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env` and set at least:

```text
OPENAI_API_KEY=...
SERVICE=payment-api
```

Start the backend:

```bash
python app.py
```

Open `http://localhost:8000`.

## Run the web UI

```bash
cd web
npm install
npm run dev
```

Open `http://localhost:3000` for the command center UI.

## Railway Deploy

This repo now uses a **single Railway service**. The root `Dockerfile` builds the Next.js UI and the FastAPI backend together, and `start.sh` launches both inside one container.

See `DEPLOYMENT.md` for the complete production deployment guide, including all required environment variables and Railway settings.

For Railway, be sure to set `PORT=8000`, `WEB_PORT=3000`, `PUBLIC_BASE_URL`, `WEB_BASE_URL`, `WEB_INTERNAL_URL=http://127.0.0.1:3000`, and `API_AUTH_TOKEN`.

In production, the Railway public URL serves:

- `/` for the dashboard
- `/incident/{incident_id}` for the incident detail page
- `/api/*` for the backend API
- `/api/slack/*` for Slack events, interactivity, and commands

After deploy, set these Railway variables for Slack:

- `SLACK_SIGNING_SECRET`
- `SLACK_BOT_TOKEN`
- `SLACK_DEFAULT_CHANNEL`
- `PUBLIC_BASE_URL` or `WEB_BASE_URL` to the Railway public domain
- `WEB_INTERNAL_URL` only if you change the internal Next.js port

Then point Slack to the Railway public domain with these paths:

- `/api/slack/commands`
- `/api/slack/events`
- `/api/slack/interactivity`

For local frontend development only, keep `NEXT_PUBLIC_API_BASE=http://localhost:8000` in the `web/` shell.

## Jarvis And Incident RAG

- `/jarvis` is the system-aware assistant page. It supports voice questions, spoken answers, and an optional typed prompt for graph-aware operational questions.
- Incident text RAG stays on the selected incident page through `POST /api/incidents/{incident_id}/ask`.
- The dashboard and knowledge graph remain exploration/operations surfaces; they do not host the broad text assistant.
- Jarvis query routing is Memgraph graph/query memory -> local Ollama Qwen 2.5 -> configured online provider -> deterministic heuristic.
- Obsidian vaults are document sources, not runtime memory. The `obsidian_vault` connector indexes Markdown notes into the knowledge DB, RAG retrieves only relevant snippets, and those retrieved snippets can be mirrored into the graph as evidence nodes.
- Uploaded logs and knowledge files are now persisted under `data/uploads/`, indexed into the knowledge base, and optionally stored in a Qdrant vector store for hybrid retrieval.
- Memgraph/query memory remains the fast runtime layer for live incident properties, query-answer cache hits, evidence links, and graph-aware traversal. This avoids sending full vault or incident history context to the LLM on every question.
- Admin lets operators configure connectors, cloud log stores, voice providers, and online LLM fallback providers such as OpenAI, Gemini, Groq, or Claude.

## Demo Scenarios

- `payment-api` - database connection pool exhaustion
- `order-processor` - memory leak and GC pressure
- `checkout-gateway` - cascading downstream timeout
- `catalog-api` - traffic surge and cost overrun
- `search-api` - cache stampede and retry amplification

### Recommended Agentic RAG Demo

Launch **Cache Stampede** from the dashboard. The investigation should conclude
that synchronized cache misses caused retry amplification, with no correlated
deployment or database-pool failure. Then use these questions to demonstrate
intent-aware retrieval across the postmortem, runbook, similar-incident guide,
service profile, and live incident evidence:

1. `Why did search-api latency jump and retries increase?`
2. `How many users and how much revenue were impacted?`
3. `How do we mitigate a Redis cache stampede right now?`
4. `Is this more similar to an organic traffic surge or DB pool exhaustion?`
5. `What controls prevent synchronized cache expiry from happening again?`

The curated retrieval inputs live in
`data/knowledge/demo_rag_documents.json`. Each chunk includes a stable ID,
knowledge kind, tags, retrieval-oriented `embedding_text`, and grounded content.

## API

- `GET /api/health`
- `GET /api/config`
- `GET /api/graph`
- `POST /api/incidents/trigger`
- `GET /api/incidents`
- `GET /api/incidents/{incident_id}`
- `GET /api/incidents/{incident_id}/trace`
- `POST /api/incidents/{incident_id}/ask`
- `POST /api/assistant/chat`
- `GET /api/jarvis/status`
- `GET /api/admin/connectors`
- `GET /api/admin/connectors/catalog`
- `GET /api/analytics/incidents`
- `GET /api/knowledge-graph`
- `POST /api/incidents/{incident_id}/review/rca`
- `POST /api/incidents/{incident_id}/review/request-more-data`
- `POST /api/incidents/{incident_id}/review/override-root-cause`
- `GET /api/incidents/{incident_id}/postmortem`
- `GET /` - Serves the Next.js dashboard through the integrated container
- `GET /incident/{incident_id}` - Serves the Next.js incident page through the integrated container

## Testing

```bash
pytest
python -m evals.run
```

All three scenarios pass with:

- Log anomalies detected > 0
- Metric anomalies detected > 0
- Root cause confidence > 0.60
- Affected users > 0
- Complete audit trail

## Results

### Scenario 1: Database Connection Pool Exhaustion

- **Root Cause**: Pool size reduced from 50 to 30 in recent deployment
- **Confidence**: 85%
- **Affected Users**: 1,400
- **Revenue Impact**: $700/minute
- **Key Indicators**: Connection timeout errors, CPU spike 23% → 78%, error_rate 0.1% → 68%

### Scenario 2: Memory Leak

- **Root Cause**: Code regression causing memory not to be released
- **Confidence**: 72%
- **Affected Users**: 5,000
- **Revenue Impact**: $1,500/minute
- **Key Indicators**: GC pause times increasing, memory 500MB → 2000MB (no deployment)

### Scenario 3: Cascading Failure

- **Root Cause**: Timeout calling downstream payment-api service
- **Confidence**: 80%
- **Affected Users**: 3,000
- **Revenue Impact**: $1,200/minute
- **Key Indicators**: Timeout errors, latency spike 55ms → 8000ms, error_rate spike to 85%

## Tech Stack

- **Python 3.10+** - Core language
- **LangGraph** - Agent orchestration
- **FastAPI** - REST backend (analysis runs as a background task; the UI polls live progress)
- **OpenAI (gpt-4o)** - LLM reasoning, configured via `OPENAI_API_KEY` in `.env`
- **Pydantic** - Data validation
- **pytest** - Testing
- **Next.js** - Clean production-ready web frontend

## API Endpoints

- `POST /api/incidents/trigger` - Start incident analysis (returns immediately with `current_status: "investigating"`; agents run in the background)
  ```json
  {
    "timestamp": "2026-07-07T14:32:15Z",
    "service": "payment-api",
    "alert_description": "Connection pool exhaustion",
    "severity": "critical"
  }
  ```

- `GET /api/incidents` - List all incidents (newest first)
- `GET /api/incidents/{incident_id}` - Poll live analysis state / retrieve completed analysis
- `GET /api/config` - Active LLM provider and model (`heuristic` when no key is set)
- `GET /api/graph` - Mermaid rendering of the agent graph
- `POST /api/incidents/{incident_id}/remediation/{step_index}/decision` - Human approval gate for recovery recommendations. High-risk approvals now trigger an optional email alert when configured.
- `POST /api/incidents/upload-log` - Upload a log file or pasted text to create a new incident from log evidence.
- `GET /api/health` - Health check

## Recent Updates and Developer Notes

### High-risk remediation approval and email alerting
- The backend now classifies remediation approval risk in `agents/authz.py`.
- When a remediation decision is approved and the local policy marks it as `high` risk, the system sends an optional alert email from `agents/notify.py::post_high_risk_email_alert`.
- Email alerting is intentionally optional and non-fatal: missing SMTP configuration will not stop incident handling or existing review flows.
- Required environment variables for email alerting:
  - `EMAIL_SMTP_HOST`
  - `EMAIL_SMTP_PORT`
  - `EMAIL_SMTP_USERNAME`
  - `EMAIL_SMTP_PASSWORD`
  - `EMAIL_USE_TLS`
  - `EMAIL_FROM` or `EMAIL_FROM_DOMAIN`
  - `HIGH_RISK_ALERT_EMAIL_TO`
- `WEB_BASE_URL` must be set correctly for incident links in email messages.
- Existing high-impact war-room and Teams alerting still use `WAR_ROOM_WEBHOOK_URL` and `TEAMS_ALERT_WEBHOOK_URL`.

### Upload-based incident creation
- The new endpoint `POST /api/incidents/upload-log` accepts a file upload or pasted text and builds an incident record from detected service, severity, and log evidence.
- The parser is implemented in `app.py` and supports common patterns such as payment DB pool exhaustion, timeout cascades, memory pressure, and cache stampedes.
- Manual upload test samples are available under `test/sample_logs/`.
- Unit tests covering the parser are in `test/tests/test_upload_log_parser.py`.

### Configuration and UI disclaimers
- The feature is additive: no frontend change is required if the UI already interacts with the backend remediation decision endpoint.
- For production UI usage, ensure `WEB_BASE_URL` points to the public dashboard domain so emailed incident links resolve correctly.
- Leave `EMAIL_SMTP_HOST` blank to disable email delivery safely in local development.
- For local Jarvis/Ollama fallback, configure `OLLAMA_ENABLED`, `OLLAMA_BASE_URL`, and `OLLAMA_MODEL` in `.env`.
- For online fallback, set `LLM_PROVIDER` and provider credentials such as `OPENAI_API_KEY`, `GEMINI_API_KEY`, or `ANTHROPIC_API_KEY`.

## Slack Assistant Bot Mode

The backend can also run as a Slack incident assistant so users can trigger,
inspect, and govern incidents from Slack threads while the web app remains the
full command center.

Configure these values in `.env`:

```bash
SLACK_SIGNING_SECRET=...
SLACK_BOT_TOKEN=xoxb-...
SLACK_DEFAULT_CHANNEL=C0123456789
PUBLIC_BASE_URL=https://your-public-railway-app.example.com
WEB_BASE_URL=https://your-public-railway-app.example.com
WEB_INTERNAL_URL=http://127.0.0.1:3000
```

Slack app URLs:

- Slash command request URL: `POST /api/slack/commands`
- Events API request URL: `POST /api/slack/events`
- Interactivity request URL: `POST /api/slack/interactivity`

Suggested bot scopes: `chat:write`, `commands`, `app_mentions:read`, and
message read scopes for the channels where the bot should answer threaded
incident questions.

Example commands:

```text
/aioc trigger db
/aioc trigger memory
/aioc status
/aioc ask <incident_id> why do we think this is the root cause?
/aioc trace <incident_id>
```

## Next Steps

## Platform Connectors and Incident Intelligence

The Admin screen at `/admin` manages MCP endpoints, AWS S3, Google Cloud
Storage, Azure Blob, live log streams, Slack, Google Drive, file sources, and
Sarvam/ElevenLabs voice provider configurations. Connector credentials are
referenced by server-side environment-variable name rather than entered into
the browser. Each connector exposes a runtime heartbeat from the same screen.

The Jarvis screen is available at `/jarvis` with multilingual browser voice
input/output plus optional typed questions. Day, week, and month incident
analytics stay on the dashboard. `/graph` provides an interactive incident
ontology that connects incidents to services, root causes, resolution classes,
and retrieved evidence, while Jarvis remains the voice/text entry point for
graph-aware questions.

Jarvis uses a local-first model stack: persistent graph/query memory first, then
local Ollama if the selected model is available, then a configured online LLM
connector, and finally a deterministic heuristic fallback. The admin page can
select local Ollama models and monitor whether the runtime is feasible; if the
configured Ollama model is not installed or the host cannot run it, the UI
warns the user and prompts for an online provider API key before failing over.

Cloud SDK ingestion and provider-native Sarvam/ElevenLabs audio are adapter
extension points: the current implementation registers and monitors those
connections while retaining browser speech as the zero-configuration runtime.

- Implement live Datadog/Splunk integration
- Add Codex-style code-change analysis for deployment diffs
- Create automated remediation playbooks
- Build Slack/PagerDuty notification system
- Implement multi-step recovery automation
- Add historical incident search and learning

## License

MIT

## Authors

Amulya Gupta & Anujay
#   A I O C  
 #   A I O C  
 #   A I O C  
 