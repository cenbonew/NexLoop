"""Run from repository root: .venv/bin/python docs/implementation/task-b-m21-g1-input/verify.py"""
from pathlib import Path
import hashlib,json,re
folder=Path(__file__).resolve().parent
root=folder.parents[2]
raw=(root/'docs/release/run-task-b-m2-1.md').read_bytes()
text=raw.decode('utf-8')
legacy=text.split('```sql',1)[1].split('```',1)[0].encode('utf-8')
actual=re.search(r'```sql\n(.*?)```',text,re.S)[1].encode('utf-8')
record=json.loads((folder/'comparison.json').read_text())
assert legacy==b'\n'+actual
for name,value,key in [('recorded-split-input.sql',legacy,'legacy_sha256'),('test-regex-input.sql',actual,'test_sha256')]:
    assert (folder/name).read_bytes()==value
    assert hashlib.sha256(value).hexdigest()==record[key]
assert hashlib.sha256(raw).hexdigest()==record['source_sha256']
print('PASS: recorded input is exactly one LF followed by test-regex input; SQL unchanged')
