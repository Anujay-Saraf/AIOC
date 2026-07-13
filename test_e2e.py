import urllib.request, json, time

# Trigger the DB Pool Exhaustion incident
payload = json.dumps({
    'timestamp': '2026-07-12T11:46:00Z',
    'service': 'payment-api',
    'alert_description': 'Database connection pool exhaustion detected',
    'severity': 'critical'
}).encode()

req = urllib.request.Request(
    'http://localhost:8000/api/incidents/trigger',
    data=payload,
    method='POST',
    headers={'Content-Type': 'application/json'}
)

with urllib.request.urlopen(req, timeout=10) as r:
    result = json.loads(r.read())

incident_id = result.get('incident_id')
print(f"Triggered! incident_id={incident_id}")
print(f"Initial status: {result.get('current_status')}")
print(f"Service: {result.get('service')}")
print()

# Poll until complete (60 seconds max)
for i in range(30):
    time.sleep(2)
    try:
        url = f'http://localhost:8000/api/incidents/{incident_id}'
        with urllib.request.urlopen(url, timeout=5) as r:
            inc = json.loads(r.read())
        status    = inc.get('current_status', '')
        steps     = inc.get('completed_steps', [])
        confidence= inc.get('rca_confidence', 0)
        lifecycle = inc.get('lifecycle_status', '')
        elapsed   = i * 2
        print(f"[{elapsed:02d}s] status={status:<28} steps={len(steps)}/11  conf={confidence:.2f}  lifecycle={lifecycle}")

        if status in ('complete', 'summary_ready', 'approval_assessed') or 'learning' in steps:
            print()
            print("=" * 60)
            print("INVESTIGATION COMPLETE")
            print("=" * 60)
            rc = inc.get('root_cause') or {}
            print(f"Root cause:      {rc.get('hypothesis', 'N/A')}")
            print(f"Confidence:      {inc.get('rca_confidence', 0):.0%}")
            print(f"Affected users:  {inc.get('affected_users', 0):,}")
            rev = inc.get('estimated_revenue_impact_per_minute', 0)
            print(f"Revenue impact:  ${rev:.2f}/min")
            print(f"Business risk:   {inc.get('business_risk_level', 'N/A')}")
            da  = inc.get('deployment_analysis') or {}
            print(f"Deployment:      {da.get('correlation_summary', 'N/A')}")
            recs = inc.get('recovery_recommendations', [])
            print(f"Recovery steps:  {len(recs)} recommendations")
            for j, r in enumerate(recs[:4], 1):
                print(f"  {j}. {str(r)[:100]}")
            print()
            print(f"Executive summary:")
            print(inc.get('executive_summary', 'N/A')[:400])
            print()
            print(f"Completed steps: {steps}")
            break
    except Exception as e:
        print(f"Poll error: {e}")
