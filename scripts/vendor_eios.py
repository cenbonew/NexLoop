#!/usr/bin/env python3
"""Copy an explicit static dependency closure; never copy a whole repository."""
import hashlib,json,shutil,sys
from pathlib import Path
root=Path(__file__).resolve().parents[1];source=Path(sys.argv[1]);base=source/'src';dest=root/'packages/eios-core/src'
rows=json.loads((root/'docs/s0/source-reuse-inventory.json').read_text())
names={'Ontology schema','Ontology registry','Ontology objects and relations','Definition Registry','Policy and intersection','Decision service','Service and agent identity','Application ceiling','Property authorization','Authorization audit','Migration runner','Local artifacts','Artifact protocol'}
files=set()
for r in rows:
 if r['capability'] not in names:continue
 files.add(r['source_path'])
 for module in r['transitive_dependencies']:
  if not module.startswith('eios.'):continue
  p=base.joinpath(*module.split('.'));p=p.with_suffix('.py') if p.with_suffix('.py').exists() else p/'__init__.py'
  if not p.is_file():
   if base.joinpath(*module.split('.')).is_dir(): continue
   raise RuntimeError(f'unresolved {module}')
  files.add(str(p.relative_to(source)))
for rel in list(files):
 for parent in (source/rel).parents:
  if parent==base:break
  init=parent/'__init__.py'
  if init.is_file():files.add(str(init.relative_to(source)))
records=[]
for rel in sorted(files):
 assert not any(x in rel for x in ('/tennis','/sports8','/tos/','/composition/'))
 p=source/rel;t=dest/p.relative_to(base)
 if t.exists() and t.read_bytes()!=p.read_bytes(): raise RuntimeError(f'refusing to overwrite adapted file: {t.relative_to(root)}')
 t.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,t)
 records.append({'source_path':rel,'destination':str(t.relative_to(root)),'upstream_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'adaptations':[]})
# Isolate lineage: only generic durable base; retain original bytes and checksums.
p=source/'src/eios/migrations/0001_durable_core.sql';t=dest/'eios/migrations/0001_durable_core.sql';t.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,t)
(t.parent/'__init__.py').write_text('"""NexLoop independent EIOS bootstrap catalog, not upstream revision 0332."""\n')
records.append({'source_path':str(p.relative_to(source)),'destination':str(t.relative_to(root)),'upstream_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'adaptations':[]})
(root/'packages/eios-core/PROVENANCE.json').write_text(json.dumps({'commit':'1e363db982daeca74058453ad12fc4a6cac333da','license':'MIT (owner authorized NexLoop publication)','files':records},indent=2)+'\n')
print(f'Copied {len(records)} explicitly selected files; no upstream history or domain migration chain')
