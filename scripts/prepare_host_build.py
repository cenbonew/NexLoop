"""Prepare a credential-free Host Docker context from an actual Node24 build."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

ROOT=Path(__file__).resolve().parents[1]
FILES={'.dockerignore','Dockerfile','agent_host.py','agent-host-main.js','package.json','runtime.tar'}


def prepare_host(output):
    output=Path(output).absolute();base=ROOT/'.ci-results'
    if base.is_symlink() or output.is_symlink() or output.exists() or not output.resolve().is_relative_to(base.resolve()):
        raise ValueError('new ignored Host build directory required')
    env={k:v for k,v in os.environ.items() if not k.startswith(('MODEL_','COMPOSE_'))}
    version=subprocess.run(['node','-p','process.versions.node'],env=env,cwd=ROOT,capture_output=True,text=True,check=True,timeout=10).stdout.strip()
    if version!='24.13.0':raise ValueError('locked Node24.13.0 build required')
    subprocess.run(['pnpm','build:pi'],env=env,cwd=ROOT,capture_output=True,check=True,timeout=120)
    subprocess.run(['pnpm','--filter','@nexloop/agent-host','build'],env=env,cwd=ROOT,capture_output=True,check=True,timeout=60)
    entry=ROOT/'apps/agent-host/dist/main.js'
    if entry.is_symlink() or not entry.is_file():raise ValueError('actual compiled Host required')
    # pnpm deploy produces an isolated production closure, including workspace Pi.
    base.mkdir(mode=0o700,exist_ok=True)
    # Remove this exact owned staging closure on success and exceptions.
    with tempfile.TemporaryDirectory(prefix='host-runtime-stage-',dir=base) as stage_directory:
        stage=Path(stage_directory)
        deployment=stage/'runtime'
        subprocess.run(['pnpm','--filter','@nexloop/agent-host','deploy','--prod','--legacy',str(deployment)],env=env,cwd=ROOT,capture_output=True,check=True,timeout=180)
        licenses=deployment/'licenses'
        licenses.mkdir()
        for package in ('ai','chord','durable','telemetry'):
            shutil.copyfile(ROOT/f'vendor/pi/packages/{package}/LICENSE',licenses/f'Pi-{package}-LICENSE')
        # pnpm legacy deploy retains one self-reference to the source workspace.
        self_link=deployment/'node_modules/.pnpm/node_modules/@nexloop/agent-host'
        if self_link.is_symlink():
            if self_link.resolve()!= (ROOT/'apps/agent-host').resolve():raise ValueError('unexpected self link')
            self_link.unlink()
            self_link.symlink_to(os.path.relpath(deployment,self_link.parent))
        if any(p.name not in {'dist','node_modules','package.json','README.md','licenses'} for p in deployment.iterdir()):
            raise ValueError('unexpected deployment root configuration')
        # Retain pnpm's internal links, rejecting escapes, special files and credentials.
        for path in deployment.rglob('*'):
            rel=path.relative_to(deployment)
            if any(part in {'.git','.env','local-only'} or part.startswith('.env.') for part in rel.parts):
                raise ValueError('private runtime package member rejected')
            if path.is_symlink():
                if path.readlink().is_absolute():raise ValueError('absolute runtime symlink rejected')
                target=path.resolve(strict=True)
                if not target.is_relative_to(deployment.resolve()):raise ValueError('runtime symlink escape rejected')
            elif not path.is_file() and not path.is_dir():raise ValueError('runtime special file rejected')
        output.mkdir(mode=0o700,parents=True)
        with tarfile.open(output/'runtime.tar','w',format=tarfile.PAX_FORMAT) as archive:
            for path in sorted(deployment.rglob('*')):
                rel=path.relative_to(deployment).as_posix()
                if rel.startswith('/') or '..' in Path(rel).parts:raise ValueError('unsafe archive member')
                info=archive.gettarinfo(str(path),arcname=rel)
                info.uid=info.gid=10001;info.uname=info.gname='nexloop'
                if info.isfile():
                    info.mode=0o755 if info.mode & 0o111 else 0o644
                    with path.open('rb') as source:archive.addfile(info,source)
                else:
                    info.mode=0o755 if info.isdir() else 0o777
                    archive.addfile(info)

        for source,name in [(ROOT/'deploy/community/Host.Dockerfile','Dockerfile'),(ROOT/'scripts/agent_host.py','agent_host.py'),(entry,'agent-host-main.js')]:
            if source.is_symlink():raise ValueError('regular owned source required')
            shutil.copyfile(source,output/name)
        (output/'package.json').write_text(json.dumps({'type':'module','private':True})+'\n')
        (output/'.dockerignore').write_text('**\n'+''.join('!'+name+'\n' for name in sorted(FILES-{'.dockerignore'})))
        return {'scope':'host-build-context-only','node_version':version,'container_built':False,
                'sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir()}}
