import urllib.request, json

url = 'http://localhost:8000/api/incidents'
with urllib.request.urlopen(url, timeout=5) as r:
    incs = json.loads(r.read())

if not incs:
    print("No incidents found")
else:
    inc = incs[-1]
    iid       = inc.get('incident_id')
    status    = inc.get('current_status')
    lifecycle = inc.get('lifecycle_status')
    steps     = inc.get('completed_steps', [])
    conf      = inc.get('rca_confidence', 0)
    users     = inc.get('affected_users', 0)
    risk      = inc.get('business_risk_level')
    rc        = inc.get('root_cause') or {}
    da        = inc.get('deployment_analysis') or {}
    recs      = inc.get('recovery_recommendations') or []
    summary   = inc.get('executive_summary') or ''

    print(f"incident_id:     {iid}")
    print(f"status:          {status}")
    print(f"lifecycle:       {lifecycle}")
    print(f"completed_steps: {steps}")
    print(f"rca_confidence:  {conf}")
    print(f"affected_users:  {users:,}")
    print(f"business_risk:   {risk}")
    print(f"root_cause:      {rc.get('hypothesis', 'not yet')}")
    print(f"deployment:      {da.get('correlation_summary', 'not yet')}")
    print(f"recovery_recs:   {len(recs)} items")
    for i, r in enumerate(recs[:4], 1):
        print(f"  {i}. {str(r)[:100]}")
    print(f"executive_summary:")
    print(summary[:400])
