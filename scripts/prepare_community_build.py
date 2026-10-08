"""Build a minimal credential-free Docker context; no daemon, secret or .env read."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[1]


def prepare(output):
    base=ROOT/'.ci-results';output=Path(output).absolute()
    if base.is_symlink() or output.is_symlink() or output.exists() or not output.resolve().is_relative_to(base.resolve()):
        raise ValueError('new build context must be below the local ignored results directory')
    with tempfile.TemporaryDirectory(prefix='nexloop-community-wheel-') as temporary:
        owned=Path(temporary)
        subprocess.run(['uv','build','--package','nexloop-eios-core','--wheel','--offline','--out-dir',str(owned)],cwd=ROOT,check=True,capture_output=True,timeout=60)
        wheels=list(owned.glob('*.whl'))
        if len(wheels)!=1 or wheels[0].name!='nexloop_eios_core-0.1.0-py3-none-any.whl':
            raise RuntimeError('unexpected core wheel')
        exported=subprocess.run(['uv','export','--frozen','--offline','--no-dev','--no-emit-workspace'],cwd=ROOT,check=True,capture_output=True,timeout=30)
        output.mkdir(mode=0o700,parents=True)
        shutil.copyfile(ROOT/'deploy/community/Dockerfile',output/'Dockerfile')
        shutil.copyfile(wheels[0],output/wheels[0].name)
        (output/'requirements.txt').write_bytes(exported.stdout)
        env={k:v for k,v in os.environ.items() if not k.startswith(('MODEL_','VITE_','COMPOSE_'))}
        subprocess.run(['pnpm','exec','node','--input-type=module','-e',"import {build} from 'vite'; await build({configFile:false,envDir:false});"],cwd=ROOT/'apps/web',env=env,check=True,capture_output=True,timeout=60)
        web=ROOT/'apps/web/dist'
        public=[p for p in web.rglob('*') if p.is_file()]
        if not public or any(p.is_symlink() or p.suffix not in {'.html','.js','.css'} for p in public):raise ValueError('public frontend output required')
        for p in public:
            target=output/'web'/p.relative_to(web);target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,target)
        (output/'.dockerignore').write_text('**\n!Dockerfile\n!requirements.txt\n!nexloop_eios_core-0.1.0-py3-none-any.whl\n!web/\n!web/**\n')
    files=sorted(str(p.relative_to(output)) for p in output.rglob('*') if p.is_file())
    if set(name for name in files if not name.startswith('web/'))!={'.dockerignore','Dockerfile','nexloop_eios_core-0.1.0-py3-none-any.whl','requirements.txt'}:
        raise RuntimeError('unexpected Docker context contents')
    return {'scope':'core-test-build-context-only','container_built':False,
        'sha256':{name:hashlib.sha256((output/name).read_bytes()).hexdigest() for name in files}}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True,type=Path);a=p.parse_args()
    try:result=prepare(a.output)
    except Exception:
        print(json.dumps({'passed':False,'error':'community build context refused or unavailable'}));return 1
    print(json.dumps({'passed':True,**result},indent=2));return 0


if __name__=='__main__':raise SystemExit(main())
