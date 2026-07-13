# Sample Upload Files for AIOC

These files can be uploaded directly to the AIOC system via **Admin → Knowledge** or **Incident → Upload Logs** sections.

## Log Files
These simulate real application logs that AIOC agents will analyze for patterns.

| File | Service | Incident Type | Format |
|------|---------|---------------|--------|
| `payment_api_incident.log` | payment-api | DB pool exhaustion + circuit breaker + GC pause | Plain text log |
| `search_api_cache_stampede.log` | search-api | Cache stampede + retry amplification + OOMKill | Plain text log |
| `order_processor_kafka_lag.jsonl` | order-processor | Kafka lag + downstream timeout + OOMKill | JSON Lines |

## Knowledge Documents
These are uploaded to the AIOC knowledge base so agents can reference them during RCA.

| File | Type | Contents |
|------|------|----------|
| `payment_api_runbook.txt` | Runbook | Step-by-step remediation for payment-api P0 incidents |
| `aioc_knowledge_base.txt` | Context | Service catalog, infra details, escalation policy, root cause patterns |

## How to Upload

### Upload a log for analysis
1. Open an incident → click **Upload log** (download icon in header)
2. Select `payment_api_incident.log` or any other `.log`/`.jsonl` file
3. The system re-runs analysis with the uploaded log as additional evidence

### Upload to knowledge base
1. Go to **Admin → Knowledge**
2. Drag and drop `payment_api_runbook.txt` or `aioc_knowledge_base.txt`
3. Agents will reference this in future RCA and recovery recommendations

## Log Format Requirements
The upload endpoint accepts **UTF-8 text files**:
- `.log` — Plain text log lines (one entry per line)
- `.txt` — Runbooks, post-mortems, operational docs
- `.jsonl` / `.json` — JSON-lines structured logs (one JSON object per line)
- `.md` — Markdown runbooks and architecture docs
- **Max size**: ~10MB (server-side UTF-8 decode limit)
