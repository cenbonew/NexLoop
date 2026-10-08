"""One-shot test-only owned PG sandbox; no supplied/persistent database DSN."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import subprocess
import tempfile

import psycopg
from psycopg.conninfo import make_conninfo

from nexloop_eios.bootstrap import bootstrap,verify
from nexloop_eios.sandbox_profile import publish_test_artifact_identity
from nexloop_eios.artifact_smoke import run_smoke
from nexloop_eios.doctor import diagnose


def _private_file(path,value):
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600),'wb') as stream:
        stream.write(value if type(value) is bytes else value.encode())


def run_sandbox(*,pg_bin):
    result={'mode':'test','world':'test','passed':False,'product_ready':False,
            'model_validation':'not_run','shutdown_confirmed':True,'cleanup_confirmed':True}
    pg_bin=Path(pg_bin).absolute()
    if any(not (pg_bin/name).is_file() for name in ('initdb','pg_ctl')):
        result['error']='PostgreSQL initdb/pg_ctl binaries required';return result
    root=Path(tempfile.mkdtemp(prefix='nexloop-core-sandbox-'))
    data=root/'data';sock=root/'socket';sock.mkdir(mode=0o700)
    started=False;initialized=False
    result['shutdown_confirmed']=False;result['cleanup_confirmed']=False
    try:
        subprocess.run([str(pg_bin/'initdb'),'-D',str(data),'-U','nexloop_bootstrap',
            '--auth=trust','--no-locale','-E','UTF8'],check=True,capture_output=True,timeout=30)
        initialized=True
        options=f"-k {shlex.quote(str(sock))} -p 5432 -c listen_addresses='' -c log_statement=none -c log_parameter_max_length=0 -c log_parameter_max_length_on_error=0"
        started=True  # Attempted start: even a timeout needs an authoritative status check.
        subprocess.run([str(pg_bin/'pg_ctl'),'-D',str(data),'-l',str(root/'postgres.log'),
            '-o',options,'-t','10','-w','start'],check=True,capture_output=True,timeout=15)
        dsn=make_conninfo(host=str(sock),port=5432,dbname='postgres',user='nexloop_bootstrap')
        key_material=secrets.token_bytes(32);key_id='sandbox-test-v1'
        with psycopg.connect(dsn,autocommit=True) as owner:
            bootstrap(owner);result['catalog']=verify(owner)
            token=publish_test_artifact_identity(owner)
            owner.execute('insert into authz.nexloop_authority_signing_keys values(%s,%s,true)',(key_id,key_material))
        app_dsn=make_conninfo(dsn,user='nexloop_api')
        _private_file(root/'dsn',app_dsn);_private_file(root/'credential',token);_private_file(root/'key',key_material)
        result['smoke']=run_smoke(database_url_file=root/'dsn',service_credential_file=root/'credential',
            signing_key_file=root/'key',signing_key_id=key_id,artifact_root=root/'artifacts')
        result['doctor']=diagnose(database_url=app_dsn,artifact_root=root/'artifacts',environment={'MODEL_PROVIDER':'test'})
        result['passed']=result['smoke']['passed'] and result['doctor']['foundation_checks_passed']
    except Exception:
        result['error']='owned test sandbox failed; no persistent database configuration is used'
    finally:
        try:
            if started:
                command=[str(pg_bin/'pg_ctl'),'-D',str(data)]
                status=subprocess.run(command+['status'],capture_output=True,timeout=5)
                if status.returncode==0:
                    subprocess.run(command+['-m','fast','-t','10','-w','stop'],check=True,capture_output=True,timeout=15)
                    status=subprocess.run(command+['status'],capture_output=True,timeout=5)
                stopped=status.returncode==3
            else:stopped=initialized
            result['shutdown_confirmed']=stopped
            if stopped:
                assert root.name.startswith('nexloop-core-sandbox-') and root.parent.resolve()==Path(tempfile.gettempdir()).resolve()
                shutil.rmtree(root);result['cleanup_confirmed']=True
            else:result['passed']=False
        except Exception:
            result['passed']=False
            result['error']='owned sandbox shutdown/cleanup could not be confirmed'
    return result


def main():
    parser=argparse.ArgumentParser(description='Create, test and retire a new private PostgreSQL/core sandbox')
    parser.add_argument('--mode',required=True,choices=['test'])
    parser.add_argument('--pg-bin',required=True,type=Path)
    args=parser.parse_args();result=run_sandbox(pg_bin=args.pg_bin)
    print(json.dumps(result,indent=2))
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
