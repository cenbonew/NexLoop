#!/usr/bin/env python3
"""Read a frozen source checkout and generate reviewable S0 evidence (no execution)."""
import ast, hashlib, json, re, subprocess, sys
from pathlib import Path
source=Path(sys.argv[1]); root=Path(__file__).resolve().parents[1]
commit=subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
assert commit=='1e363db982daeca74058453ad12fc4a6cac333da'
entries=[
 ('Ontology schema','src/eios/ontology/models.py','keep'),
 ('Ontology registry','src/eios/adapters/postgres/ontology_registry.py','adapt'),
 ('Ontology objects and relations','src/eios/adapters/postgres/ontology_store.py','adapt'),
 ('Definition Registry','src/eios/ontology/definitions.py','keep'),
 ('Capability Registry','src/eios/capabilities/registry.py','keep'),
 ('Action governance','src/eios/actions/governance.py','adapt'),
 ('Policy and intersection','src/eios/authz/intersection.py','keep'),
 ('Decision service','src/eios/authz/service.py','keep'),
 ('Service and agent identity','src/eios/identity/models.py','keep'),
 ('Application ceiling','src/eios/authz/applications.py','keep'),
 ('Property authorization','src/eios/authz/property_security.py','adapt'),
 ('Tenant and UnitOfWork','src/eios/adapters/postgres/database.py','adapt'),
 ('Job lease and fencing','src/eios/adapters/postgres/async_runtime.py','adapt'),
 ('Outbox','src/eios/adapters/postgres/outbox.py','adapt'),
 ('Artifact protocol','src/eios/kernel/ports/artifacts.py','keep'),
 ('Artifact metadata','src/eios/adapters/postgres/artifacts.py','adapt'),
 ('Migration runner','src/eios/adapters/postgres/migrations.py','adapt'),
 ('Exact revision','src/eios/persistence/revision.py','adapt'),
 ('Authorization audit','src/eios/authz/audit.py','keep'),
 ('Permit 0061','src/eios/migrations/0061_authorization_governance.sql','adapt'),
 ('API Key 0313','src/eios/migrations/0313_api_key_capability_execution.sql','adapt'),
 ('Tennis effects','src/eios/actions/tennis_effect_start.py','exclude'),
 ('Local artifacts','src/eios/adapters/local/artifacts.py','adapt'),
 ('TOS artifacts','src/eios/adapters/tos/storage.py','exclude'),
 ('Approval evidence resolver','src/eios/composition/action_approval.py','adapt'),
]
# Resolve local imports recursively, including relative imports and package initializers.
base=source/'src'
def module_file(name):
 p=base.joinpath(*name.split('.'))
 return p.with_suffix('.py') if p.with_suffix('.py').is_file() else p/'__init__.py'
def imports(path):
 if path.suffix!='.py': return []
 tree=ast.parse(path.read_text()); mod='.'.join(path.relative_to(base).with_suffix('').parts)
 package=mod.rsplit('.',1)[0]
 out=set()
 for node in ast.walk(tree):
  if isinstance(node,ast.Import): out.update(a.name for a in node.names)
  elif isinstance(node,ast.ImportFrom):
   prefix='.'.join(package.split('.')[:len(package.split('.'))-node.level+1]) if node.level else ''
   name='.'.join(x for x in (prefix,node.module) if x);out.add(name)
   for a in node.names:
    child=name+'.'+a.name
    if module_file(child).is_file():out.add(child)
 return sorted(out-{'__future__'})
def closure(path):
 if path.suffix=='.sql':
  body=path.read_text()
  declared=set(re.findall(r'create (?:or replace )?(?:function|table)\s+([\w.]+)',body,re.I))
  return sorted(set(re.findall(r'\b(?:authz|control|ontology|runtime|public)\.[a-z][a-z0-9_]*\b',body))-declared)
 pending=imports(path);seen=set()
 module='.'.join(path.relative_to(base).with_suffix('').parts)
 pending.extend('.'.join(module.split('.')[:i]) for i in range(1,len(module.split('.'))))
 while pending:
  name=pending.pop()
  if name in seen:continue
  seen.add(name)
  if name.startswith('eios'):
   p=module_file(name)
   if p.is_file():
    pending.extend(imports(p))
    pending.extend('.'.join(name.split('.')[:i]) for i in range(1,len(name.split('.'))))
 return sorted(seen)
rows=[]
for name,rel,decision in entries:
 p=source/rel
 if not p.exists():
  candidates=list((source/'src/eios').rglob(Path(rel).name))
  raise RuntimeError(f'path must be resolved explicitly: {rel}: {candidates}')
 if p.suffix=='.py':
  tree=ast.parse(p.read_text()); interfaces=[n.name for n in tree.body if isinstance(n,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)) and not n.name.startswith('_')]
 else: interfaces=re.findall(r'create (?:or replace )?function\s+([\w.]+)',p.read_text(),re.I)
 needle=p.stem
 tests=[str(t.relative_to(source)) for t in (source/'tests').rglob('test*.py') if needle in t.name or ('eios.'+'.'.join(p.relative_to(base).with_suffix('').parts[1:])) in t.read_text(errors='replace')]
 rows.append(dict(capability=name,source_path=rel,commit=commit,sha256=hashlib.sha256(p.read_bytes()).hexdigest(),public_interface=interfaces,transitive_dependencies=closure(p),decision=decision,tests=tests,licensing='Owner authorized publication; no upstream LICENSE/NOTICE. NexLoop MIT attribution; third-party dependency licenses retained separately.'))
 if name=='Approval evidence resolver':
  rows[-1]['adaptation']='Remove composition package eager production storage initialization and implicit Memory verifier; explicit trusted verifier required. Production PG approval store remains unassembled.'
  original_base=base
  try:
   base=root/'packages/eios-core/src'
   adapted_path=base/'eios/composition/action_approval.py'
   if adapted_path.is_file():rows[-1]['adapted_transitive_dependencies']=closure(adapted_path)
  finally:base=original_base
(root/'docs/s0/source-reuse-inventory.json').write_text(json.dumps(rows,indent=2,ensure_ascii=False)+'\n')
md='# EIOS source reuse inventory\n\nFrozen source: `'+commit+'`. Generated by `scripts/audit_eios.py`; Python transitive imports include package initializers; SQL entries list external qualified dependency references requiring catalog review. Static analysis is not proof of runtime compatibility. Tests listed are upstream candidates, not passed NexLoop tests.\n\n'
for r in rows:
 md+=f"## {r['capability']} ({r['decision']})\n\n- source_path: `{r['source_path']}`\n- commit: `{r['commit']}`\n- public_interface: "+', '.join('`'+s+'`' for s in r['public_interface'])+'\n- transitive_dependencies: see corresponding JSON entry (complete static import closure).\n- tests: '+(', '.join('`'+s+'`' for s in r['tests']) or 'No matching test located; new coverage required.')+'\n- licensing: '+r['licensing']+'\n\n'
(root/'docs/s0/source-reuse-inventory.md').write_text(md)
# Full catalog checksum and domain exclusion list: do not copy historical SQL wholesale.
migrations=[]
for p in sorted((source/'src/eios/migrations').glob('*.sql')):
 if not re.fullmatch(r'\d{4}_[a-z][a-z0-9_]*\.sql',p.name):continue
 text=p.read_text(); domain=bool(re.search(r'tennis|sports8|easyclub|yundong8|tos_|\btos\b',p.name+'\n'+text,re.I))
 migrations.append(dict(source_path=str(p.relative_to(source)),sha256=hashlib.sha256(p.read_bytes()).hexdigest(),decision='exclude_from_bootstrap' if domain else 'review_for_extraction',reason='Domain/vendor coupling in SQL body' if domain else 'Generic candidate; requires dependency and authorization review'))
(root/'docs/s0/migration-catalog.json').write_text(json.dumps(migrations,indent=2)+'\n')
print(f'{len(rows)} capabilities; {len(migrations)} migrations; {sum(m["decision"]=="exclude_from_bootstrap" for m in migrations)} domain-coupled exclusions')
