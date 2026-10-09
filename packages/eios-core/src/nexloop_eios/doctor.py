"""Read-only startup inventory; never migrate or imply product readiness."""
import argparse
import json
import os
from pathlib import Path
import stat
import psycopg
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.bootstrap import catalog,verify
from nexloop_eios.model_profile import load_model_profile
from nexloop_eios.private_configuration import read_private_text


def diagnose(*,database_url,artifact_root,environment=None):
    checks={};details={}
    try:
        migrations=catalog();checks['catalog']=True;details['expected_revision']=migrations[-1].version
    except Exception:checks['catalog']=False
    try:
        if not database_url:raise ValueError()
        with psycopg.connect(database_url,connect_timeout=5) as c,c.transaction():
            c.execute('set transaction read only')
            details['database_role']=verify_application_role(c)
            details['database_catalog']=verify(c)
            # Hybrid recall needs pgvector and pg_trgm in the extensions schema.
            details['database_extensions']={name:version for name,version in c.execute(
                "select e.extname,e.extversion from pg_extension e join pg_namespace n on n.oid=e.extnamespace "
                "where n.nspname='extensions' and e.extname in ('vector','pg_trgm') order by e.extname").fetchall()}
        checks['postgres']=True
        checks['search_extensions']=set(details['database_extensions'])=={'vector','pg_trgm'}
    except Exception:checks['postgres']=False;checks['search_extensions']=False
    try:
        fd=os.open(Path(artifact_root),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            info=os.fstat(fd)
            checks['artifact_directory']=stat.S_ISDIR(info.st_mode) and info.st_uid==os.geteuid() and not info.st_mode&0o077
        finally:os.close(fd)
    except Exception:checks['artifact_directory']=False
    try:
        details['model']=load_model_profile(environment).public_configuration();checks['model_configuration']=True
    except Exception:checks['model_configuration']=False
    return {'checks':checks,'details':details,'foundation_checks_passed':all(checks.values()),
            'product_ready':False,'not_verified':['governed_instance_write','real_effect_action','pi_run_recovery','artifact_write_smoke','community_compose']}


def main():
    parser=argparse.ArgumentParser(description='Read-only NexLoop foundation doctor')
    parser.add_argument('--artifact-root',required=True)
    parser.add_argument('--database-url-file',type=Path)
    parser.add_argument('--mode',choices=['test'])
    args=parser.parse_args()
    configured_file=args.database_url_file or os.environ.get('DATABASE_URL_FILE')
    database_url='';configured=True
    if configured_file:
        source='private_file'
        try:database_url=read_private_text(configured_file,maximum=16384)
        except Exception:configured=False
    else:
        source='environment'
        database_url=os.environ.get('NEXLOOP_DATABASE_URL','')
        configured=bool(database_url.strip())
    result=diagnose(database_url=database_url,artifact_root=args.artifact_root,
        environment={'MODEL_PROVIDER':'test'} if args.mode=='test' else None)
    result['checks']['database_configuration']=configured
    result['details']['database_configuration_source']=source
    result['details']['mode']=args.mode or 'configured'
    result['foundation_checks_passed']=all(result['checks'].values())
    print(json.dumps(result,indent=2))
    return 0 if result['foundation_checks_passed'] else 1

if __name__=='__main__':raise SystemExit(main())
