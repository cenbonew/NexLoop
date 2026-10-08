import json
from pathlib import Path
import subprocess
from jsonschema import Draft202012Validator,FormatChecker

ROOT=Path(__file__).resolve().parents[1]


def test_generation_reproducible_and_exact_runtime_schemas():
    result=subprocess.run(['uv','run','--frozen','python','scripts/generate_contracts.py','--check'],cwd=ROOT,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
    document=json.loads((ROOT/'packages/contracts/generated/openapi-components.json').read_text())
    assert document['openapi']=='3.1.0' and document['paths']=={}
    paths=list((ROOT/'packages/contracts').glob('*.schema.json'))
    canonical_ids=[json.loads(p.read_text())['$id'] for p in paths]
    assert len(set(canonical_ids))==len(paths)
    assert set(document['x-canonical-source-sha256'])=={p.name for p in paths}
    assert set(document['components']['schemas'])=={''.join(x.title() for x in p.name.removesuffix('.schema.json').split('-')) for p in paths}
    for name,schema in document['components']['schemas'].items():
        canonical=next(json.loads(p.read_text()) for p in (ROOT/'packages/contracts').glob('*.schema.json') if ''.join(x.title() for x in p.stem.removesuffix('.schema').split('-'))==name)
        assert schema==canonical
        Draft202012Validator.check_schema(schema)
    # Conditional and format rules must survive generation without flattening.
    run=document['components']['schemas']['RunCommand']
    assert run['allOf'] and run['properties']['run_id']['format']=='uuid'
    assert not Draft202012Validator(run,format_checker=FormatChecker()).is_valid({})


def test_generated_types_compile_with_actual_typescript():
    result=subprocess.run(['pnpm','exec','tsc','--noEmit','--strict','--skipLibCheck','--target','ES2022','packages/contracts/generated/contracts.ts'],cwd=ROOT,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
