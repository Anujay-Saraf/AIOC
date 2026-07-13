import urllib.request, json

# Analytics
for period in ['day', 'week', 'month']:
    with urllib.request.urlopen(f'http://localhost:8000/api/analytics/incidents?period={period}', timeout=5) as r:
        data = json.loads(r.read())
        total = data.get('total')
        active = data.get('active')
        resolved = data.get('resolved')
        synth = data.get('synthetic_count')
        print(f"{period}: total={total} active={active} resolved={resolved} synthetic={synth}")

# Knowledge graph
print()
with urllib.request.urlopen('http://localhost:8000/api/knowledge-graph', timeout=5) as r:
    kg = json.loads(r.read())
nodes = kg.get('nodes', [])
print(f"knowledge-graph: {len(nodes)} nodes, backend={kg.get('backend')}")
if nodes:
    print(f"  Sample node: {nodes[0]}")

# Jarvis status
print()
with urllib.request.urlopen('http://localhost:8000/api/jarvis/status', timeout=5) as r:
    js = json.loads(r.read())
print(f"jarvis/status: model={js.get('model')} provider={js.get('provider')}")

# Test assistant chat
print()
import urllib.parse
payload = json.dumps({'message': 'What is the system status?'}).encode()
req = urllib.request.Request('http://localhost:8000/api/assistant/chat', data=payload, method='POST', headers={'Content-Type': 'application/json'})
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        ans = json.loads(r.read())
        print(f"chat OK: answer={str(ans.get('answer',''))[:100]}")
except Exception as e:
    print(f"chat ERROR: {e}")
