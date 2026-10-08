import hashlib, importlib, json
from pathlib import Path

def test_selected_source_integrity_and_imports():
    root=Path(__file__).resolve().parents[1]
    doc=json.loads((root/'packages/eios-core/PROVENANCE.json').read_text())
    for record in doc['files']:
        path=root/record['destination']
        assert hashlib.sha256(path.read_bytes()).hexdigest()==record['nexloop_sha256'], record['destination']
        if path.suffix=='.py' and (not path.name.startswith('_') or path.name=='__init__.py'):
            relative=path.relative_to(root/'packages/eios-core/src').with_suffix('')
            parts=list(relative.parts)
            if parts[-1]=='__init__':parts.pop()
            importlib.import_module('.'.join(parts))


def test_upstream_action_suite_provenance():
    root=Path(__file__).resolve().parents[1]
    doc=json.loads((root/'packages/eios-core/UPSTREAM_ACTION_TESTS.json').read_text())
    for record in doc['tests']:
        assert hashlib.sha256((root/record['destination']).read_bytes()).hexdigest()==record['nexloop_sha256']


def test_upstream_identity_suite_provenance():
    root=Path(__file__).resolve().parents[1]
    doc=json.loads((root/'packages/eios-core/UPSTREAM_IDENTITY_TESTS.json').read_text())
    for record in doc['tests']:
        assert hashlib.sha256((root/record['destination']).read_bytes()).hexdigest()==record['nexloop_sha256']
