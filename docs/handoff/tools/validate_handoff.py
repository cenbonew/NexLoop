#!/usr/bin/env python3
"""Static validation of the NexLoop handoff package, NOT product validation.

Requires Python >=3.11, jsonschema and PyYAML. Does not connect to any host,
execute Compose, run a model, deploy software, or invoke real-world Actions.
"""
from __future__ import annotations
import copy
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

try:
    import yaml
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError as exc:
    raise SystemExit(f"Missing static-validator dependency: {exc}. Install jsonschema and PyYAML in an isolated environment.")

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "planning/handoff-validation.json"
MANIFEST = ROOT / "MANIFEST.json"
TASK_STATUSES = {"not_started", "in_progress", "blocked", "done"}
ACCEPTANCE_STATUSES = {"not_run", "passed", "failed", "blocked", "skipped"}
EVIDENCE_KEYS = {"type", "ref", "summary", "recorded_at"}
checks: list[dict] = []


def manifest_entries() -> list[dict]:
    """Every file except MANIFEST itself and the volatile validation report."""
    import hashlib
    entries = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path in (MANIFEST, REPORT) or "__pycache__" in path.parts or path.name == ".DS_Store":
            continue
        data = path.read_bytes()
        entries.append({"path": path.relative_to(ROOT).as_posix(), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return entries


def write_manifest(document_version: str) -> None:
    entries = manifest_entries()
    MANIFEST.write_text(json.dumps({
        "project": "NexLoop",
        "document_version": document_version,
        "scope": "handoff_specification_not_application",
        "excluded_from_hashing": [MANIFEST.name, REPORT.relative_to(ROOT).as_posix()],
        "files": entries,
        "file_count_excluding_manifest": len(entries),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def check(name: str, passed: bool, detail: str = "") -> None:
    checks.append({"check": name, "passed": bool(passed), "detail": detail})

def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))

def main() -> int:
    if '--write-manifest' in sys.argv:
        version = load(MANIFEST).get('document_version', '1.0') if MANIFEST.exists() else '1.0'
        write_manifest(version)
        print(f'MANIFEST.json rewritten ({len(manifest_entries())} files, document_version={version})')
    for path in sorted(ROOT.rglob("*.json")):
        if path == REPORT or path.name == 'MANIFEST.json':
            continue
        try:
            load(path)
            check(f"json:{path.relative_to(ROOT)}", True)
        except Exception as exc:
            check(f"json:{path.relative_to(ROOT)}", False, str(exc))
    # --planning DIR lets the implementation repository validate its live planning/ copy
    # while schemas/deploy/docs are still read from the frozen handoff copy.
    planning_dir = ROOT / 'planning'
    if '--planning' in sys.argv:
        planning_dir = Path(sys.argv[sys.argv.index('--planning') + 1]).resolve()
    modules = load(planning_dir / 'modules.json')
    tasks = load(planning_dir / 'tasks.json')
    cases = load(planning_dir / 'acceptance-tests.json')
    mids = {m['id'] for m in modules}
    tids = {t['id'] for t in tasks}
    aids = {a['id'] for a in cases}
    check('module_catalog_unique_and_count', len(mids) == len(modules) == 23)
    check('task_catalog_unique_and_count', len(tids) == len(tasks) == 43)
    check('acceptance_catalog_unique_and_count', len(aids) == len(cases) == 60)
    check('tasks_modules_valid', all(set(t['modules']) <= mids for t in tasks))
    check('acceptance_modules_valid', all(a['module'] in mids for a in cases))
    check('all_modules_have_tasks', mids <= {m for t in tasks for m in t['modules']})
    check('all_modules_have_acceptance', mids <= {a['module'] for a in cases})
    check('dependencies_exist', all(set(t['dependencies']) <= tids for t in tasks))
    byid = {t['id']: t for t in tasks}
    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(tid: str):
        if tid in visiting: raise ValueError(f'cycle at {tid}')
        if tid in visited: return
        visiting.add(tid)
        for dependency in byid[tid]['dependencies']: visit(dependency)
        visiting.remove(tid); visited.add(tid)
    try:
        for tid in tids: visit(tid)
        check('task_dependency_DAG', True)
    except (ValueError, KeyError) as exc:
        check('task_dependency_DAG', False, str(exc))
    check('dependency_stage_order', all(int(byid[d]['stage'][1:]) <= int(t['stage'][1:]) for t in tasks for d in t['dependencies']))
    # Status vocabularies are fixed; progress is allowed, but a completed/passed
    # entry must carry evidence. Do not revert real progress to satisfy this tool.
    check('task_status_vocabulary', all(t['status'] in TASK_STATUSES for t in tasks),
          ','.join(f"{t['id']}={t['status']}" for t in tasks if t['status'] not in TASK_STATUSES))
    check('acceptance_status_vocabulary', all(a['status'] in ACCEPTANCE_STATUSES for a in cases),
          ','.join(f"{a['id']}={a['status']}" for a in cases if a['status'] not in ACCEPTANCE_STATUSES))
    check('done_tasks_have_evidence', all(t['evidence'] for t in tasks if t['status']=='done'),
          ','.join(t['id'] for t in tasks if t['status']=='done' and not t['evidence']))
    check('passed_or_failed_cases_have_evidence', all(a.get('evidence') for a in cases if a['status'] in ('passed','failed')),
          ','.join(a['id'] for a in cases if a['status'] in ('passed','failed') and not a.get('evidence')))
    check('evidence_entries_well_formed', all(isinstance(e,dict) and EVIDENCE_KEYS <= set(e) for t in tasks for e in t['evidence']) and all(isinstance(e,dict) and EVIDENCE_KEYS <= set(e) for a in cases for e in a.get('evidence',[])))

    schemas = {}
    examples = {}
    for path in sorted((ROOT/'contracts').glob('*.schema.json')):
        name=path.name.removesuffix('.schema.json')
        s=load(path)
        try:
            Draft202012Validator.check_schema(s)
            schemas[name]=Draft202012Validator(s,format_checker=FormatChecker())
            example_path=ROOT/f'contracts/examples/{name}.valid.json'
            example=load(example_path); examples[name]=example
            schemas[name].validate(example)
            check(f'schema_and_example:{name}',True)
        except Exception as exc:
            check(f'schema_and_example:{name}',False,str(exc))
    negative = []
    def reject(name, label, modify):
        payload=copy.deepcopy(examples[name]); modify(payload)
        errors=list(schemas[name].iter_errors(payload))
        check(f'negative_schema:{label}', bool(errors))
        negative.append(label)
    reject('event-envelope','real_world_mismatch',lambda p:p.update(mode='real'))
    reject('event-envelope','nonreal_world_mismatch',lambda p:p.update(world_id='real'))
    reject('event-envelope','invalid_date',lambda p:p.update(occurred_at='yesterday'))
    reject('action-intent','extra_identity_escalation',lambda p:p.update(scopes=['admin']))
    reject('action-intent','missing_expected_resources',lambda p:p.update(expected_versions=[]))
    reject('action-intent','invalid_digest',lambda p:p.update(payload_digest='not-a-hash'))
    reject('ontology-mutation','raw_sql_operation',lambda p:p['operations'][0].update(op='execute_sql'))
    reject('ontology-mutation','missing_property_revision',lambda p:p['operations'][0].update(expected_revision=None))
    reject('run-command','unbounded_turns',lambda p:p['budget'].update(maximum_model_turns=100000))
    reject('run-command','plaintext_secret_field',lambda p:p.update(api_key='example-only-not-a-key'))
    reject('evolution-candidate','candidate_self_approves',lambda p:p.update(publication_permit_ref='permit:admin'))

    for path in sorted((ROOT/'deploy').rglob('*.yaml')):
        try:
            content=yaml.safe_load(path.read_text(encoding='utf-8'))
            check(f'yaml_parse:{path.relative_to(ROOT)}',isinstance(content,dict))
            for name, service in content.get('services',{}).items():
                image=service.get('image','')
                check(f'image_requires_explicit_ref:{name}',isinstance(image,str) and ':?' in image)
        except Exception as exc: check(f'yaml_parse:{path.relative_to(ROOT)}',False,str(exc))
    broken=[]
    link_count=0
    for path in ROOT.rglob('*.md'):
        text=path.read_text(encoding='utf-8')
        for dest in re.findall(r'(?<!!)\[[^\]]*\]\(([^\s)]+)(?:\s+"[^"]*")?\)',text):
            if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:',dest) or dest.startswith('#'):continue
            local=unquote(dest.split('#')[0])
            if local:
                link_count+=1
                if not (path.parent/local).exists():broken.append(f'{path.relative_to(ROOT)} -> {dest}')
    check('markdown_local_links',not broken,f'{link_count} checked; '+ '; '.join(broken))
    leaked=[]
    for path in ROOT.rglob('*'):
        if not path.is_file() or path == REPORT or path.name == 'MANIFEST.json' or 'local-only' in path.parts or path.suffix=='.py':continue
        try: text=path.read_text(encoding='utf-8')
        except UnicodeError:continue
        if re.search(r'192\.168\.31\.(?:140|73)(?!\d)',text):leaked.append(str(path.relative_to(ROOT)))
    check('private_host_addresses_confined_to_private_annex',not leaked,','.join(leaked))
    # Syntactic placeholders are expected in non-deployable configuration templates.
    versions=load(planning_dir/'dependency-candidates.json')
    check('no_invented_image_digests', versions.get('image_digests') == {})
    for key in ('eios_source_commit','pi_source_commit','evo_source_commit'):
        check(f'upstream_commit_format:{key}', bool(re.fullmatch('[a-f0-9]{40}',versions[key])))
    try:
        manifest=load(MANIFEST)
        expected={e['path']:e['sha256'] for e in manifest['files']}
        actual={e['path']:e['sha256'] for e in manifest_entries()}
        drift=sorted(set(expected)^set(actual))+sorted(p for p in expected if p in actual and expected[p]!=actual[p])
        check('manifest_integrity',not drift,'; '.join(drift) + (' (run: python3 tools/validate_handoff.py --write-manifest)' if drift else ''))
    except Exception as exc:
        check('manifest_integrity',False,str(exc))
    # date-time negative test is meaningful only if the format checker knows the format.
    check('format_checker_covers_date_time', 'date-time' in FormatChecker().checkers, 'install rfc3339-validator; schemas also carry a regex pattern as a fallback')

    report={
      'scope':'static_handoff_document_validation_only',
      'generated_at':datetime.now(timezone.utc).isoformat(),
      'passed':all(c['passed'] for c in checks),
      'counts':{'modules':len(modules),'implementation_tasks':len(tasks),'product_acceptance_scenarios':len(cases),'json_schemas':len(schemas),'schema_negative_tests':len(negative),'static_checks':len(checks)},
      'checks':checks,
      'not_executed':['application_implementation','EIOS_source_extraction_or_integration','Pi_storage_conformance','container_compose_runtime_validation','server_login_or_inventory','network_TLS_ACL_tests','real_model_or_connector_calls','business_acceptance_tests','backup_restore_drill'],
      'notes':['A successful result does not establish production readiness, security compliance, deployment success, or business growth.','Task/acceptance statuses use fixed vocabularies; done/passed entries must carry evidence.','Dependency locks and image digests are deliberately unresolved candidates.']
    }
    # Live implementation checks must not mutate the frozen handoff report.
    report_path = planning_dir / 'handoff-validation.json'
    report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'counts':report['counts'],'failures':[c for c in checks if not c['passed']]},ensure_ascii=False,indent=2))
    return 0 if report['passed'] else 1

if __name__=='__main__':
    sys.exit(main())
