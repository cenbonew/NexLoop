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
            # NX-047 outbound recorder: the governed path exists (system catalog only, no business rows).
            details['outbound_recorder']=dict(zip(('outbound_records','delivery_events','recorder_command','read_derivation'),c.execute(
                "select exists(select 1 from pg_class r join pg_namespace n on n.oid=r.relnamespace where n.nspname='runtime' and r.relname='nexloop_outbound_messages'),"
                "exists(select 1 from pg_class r join pg_namespace n on n.oid=r.relnamespace where n.nspname='runtime' and r.relname='nexloop_outbound_delivery_events'),"
                "exists(select 1 from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname='authz' and p.proname='nexloop_outbound_message_command'),"
                "exists(select 1 from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname='authz' and p.proname='nexloop_assert_derived_message_read_inbound_v0077')").fetchone()))
        checks['postgres']=True
        checks['search_extensions']=set(details['database_extensions'])=={'vector','pg_trgm'}
        checks['outbound_recorder_schema']=all(details['outbound_recorder'].values())
    except Exception:checks['postgres']=False;checks['search_extensions']=False;checks['outbound_recorder_schema']=False
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


def agent_host_concurrency(config_file):
    """ADR-022 §4: the deployed Agent Host runs at most 4 Runs at once (pre-start gate)."""
    from nexloop_eios import host_concurrency
    try:
        result=host_concurrency.evaluate(host_concurrency.load(read_private_text(config_file,maximum=32768)))
    except Exception as error:
        reason=str(error) if isinstance(error,host_concurrency.HostConcurrencyInvalid) else 'Agent Host configuration is unavailable'
        return {'checks':{'agent_host_concurrency':False},'details':{'agent_host_concurrency_error':reason}}
    return {'checks':{'agent_host_concurrency':result['passed']},'details':{'agent_host_concurrency':result['details']}}


def main():
    parser=argparse.ArgumentParser(description='Read-only NexLoop foundation doctor')
    parser.add_argument('--artifact-root')
    parser.add_argument('--database-url-file',type=Path)
    parser.add_argument('--mode',choices=['test'])
    parser.add_argument('--agent-host-config',type=Path,help='private Agent Host runtime configuration (maximum_active_runs <= 4)')
    parser.add_argument('--agent-host-only',action='store_true',help='only the Agent Host concurrency check (Agent Host pre-start gate)')
    args=parser.parse_args()
    if args.agent_host_only:
        if not args.agent_host_config:parser.error('--agent-host-only requires --agent-host-config')
        result=agent_host_concurrency(args.agent_host_config)
        result.update(foundation_checks_passed=all(result['checks'].values()),product_ready=False)
        print(json.dumps(result,indent=2))
        return 0 if result['foundation_checks_passed'] else 1
    if not args.artifact_root:parser.error('--artifact-root is required')
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
    if args.agent_host_config:
        host=agent_host_concurrency(args.agent_host_config)
        result['checks'].update(host['checks']);result['details'].update(host['details'])
    result['checks']['database_configuration']=configured
    result['details']['database_configuration_source']=source
    result['details']['mode']=args.mode or 'configured'
    result['foundation_checks_passed']=all(result['checks'].values())
    print(json.dumps(result,indent=2))
    return 0 if result['foundation_checks_passed'] else 1

if __name__=='__main__':raise SystemExit(main())
