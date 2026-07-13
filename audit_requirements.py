import ast
import re
import sys
from pathlib import Path

root = Path('.')
req = set()
for line in open('requirements.txt', encoding='utf-8'):
    line = line.strip()
    if not line or line.startswith('#'):
        continue
    pkg = re.split('[>=<]', line, 1)[0].strip().lower().replace('_', '-')
    req.add(pkg)

req_test = set()
if Path('test/requirements.txt').exists():
    for line in open('test/requirements.txt', encoding='utf-8'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        pkg = re.split('[>=<]', line, 1)[0].strip().lower().replace('_', '-')
        req_test.add(pkg)

imports = set()
for p in root.rglob('*.py'):
    if any(part in {'.venv', 'venv', 'node_modules', '.git'} for part in p.parts):
        continue
    src = p.read_text(encoding='utf-8', errors='ignore')
    try:
        tree = ast.parse(src)
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                imports.add(n.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split('.')[0])

stdlib = set(sys.stdlib_module_names)
stdlib |= {
    'asyncio', 'typing', 'dataclasses', 'pathlib', 'sqlite3', 'http', 'httpx', 'ssl',
    'json', 'os', 're', 'time', 'uuid', 'sys', 'inspect', 'email', 'logging', 'hashlib',
    'subprocess', 'platform', 'multiprocessing', 'threading', 'functools', 'itertools',
    'math', 'random', 'datetime', 'socket', 'types', 'typing_extensions', 'builtins'
}
known_stdlib = set(m.lower() for m in stdlib)
third = sorted(m for m in imports if m not in known_stdlib and m not in {'__future__'})
module_package_aliases = {
    'jwt': 'pyjwt',
    'dotenv': 'python-dotenv',
    'sklearn': 'scikit-learn',
}
local_packages = {p.name for p in Path('.').iterdir() if p.is_dir()} | {p.stem for p in Path('.').glob('*.py')}

missing_main = []
missing_all = []
for m in third:
    if m in local_packages:
        continue
    normalized = module_package_aliases.get(m, m.replace('_', '-'))
    if normalized not in req:
        missing_main.append(m)
    if normalized not in (req | req_test):
        missing_all.append(m)

print('REQUIREMENTS:', sorted(req))
print('TEST REQUIREMENTS:', sorted(req_test))
print('IMPORTS:', sorted(third))
print('LOCAL PACKAGES:', sorted(local_packages))
print('MISSING MAIN:', missing_main)
print('MISSING ALL:', missing_all)
