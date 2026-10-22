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


def test_upstream_authz_suite_provenance():
    root=Path(__file__).resolve().parents[1]
    doc=json.loads((root/'packages/eios-core/UPSTREAM_AUTHZ_TESTS.json').read_text())
    for record in doc['tests']:
        assert hashlib.sha256((root/record['destination']).read_bytes()).hexdigest()==record['nexloop_sha256']
        # An adapted copy says how; a verbatim copy has no adaptations.
        assert (record['upstream_sha256']!=record['nexloop_sha256'])==bool(record['adaptations']), record['destination']


def test_every_upstream_test_file_is_recorded():
    root=Path(__file__).resolve().parents[1]
    lock=json.loads((root/'versions.lock.json').read_text())['packages']['nexloop-eios-core']['test_provenance']
    recorded={record['destination'] for manifest in lock for record in json.loads((root/manifest).read_text())['tests']}
    present={str(p.relative_to(root)) for p in (*(root/'tests/upstream').glob('*.py'),*(root/'tests').glob('test_upstream_*.py'))}
    assert present==recorded
def test_upstream_adapter_suite_provenance():
    root=Path(__file__).resolve().parents[1]
    doc=json.loads((root/'packages/eios-core/UPSTREAM_ADAPTER_TESTS.json').read_text())
    assert doc['commit']==json.loads((root/'packages/eios-core/PROVENANCE.json').read_text())['commit']
    for record in doc['tests']:
        assert hashlib.sha256((root/record['destination']).read_bytes()).hexdigest()==record['nexloop_sha256']
