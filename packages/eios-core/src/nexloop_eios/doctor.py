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


def connection_budget(*,budget_file,database_url):
    """ADR-024 / 11_DEPLOYMENT §6: declared pools (sum <= 60) and the server's max_connections (>= 80)."""
    from nexloop_eios import connection_budget as budget
    max_connections=None
    try:
        with psycopg.connect(database_url,connect_timeout=5) as c:
            max_connections=int(c.execute('show max_connections').fetchone()[0])
    except Exception:pass
    try:
        result=budget.evaluate(budget.load(budget_file),max_connections=max_connections)
    except budget.BudgetInvalid as error:
        return {'checks':{'connection_budget_file':False},'details':{'connection_budget_error':str(error)}}
    checks={'connection_budget_file':True,'connection_budget_total':result['checks']['application_pool_total'],
        'max_connections':result['checks'].get('max_connections',False)}
    return {'checks':checks,'details':{'connection_budget':result['details']}}


def agent_host_concurrency(config_file):
    """ADR-022 §4: the deployed Agent Host runs at most 4 Runs at once (pre-start gate)."""
    from nexloop_eios import host_concurrency
    try:
        result=host_concurrency.evaluate(host_concurrency.load(read_private_text(config_file,maximum=32768)))
    except Exception as error:
        reason=str(error) if isinstance(error,host_concurrency.HostConcurrencyInvalid) else 'Agent Host configuration is unavailable'
        return {'checks':{'agent_host_concurrency':False},'details':{'agent_host_concurrency_error':reason}}
    return {'checks':{'agent_host_concurrency':result['passed']},'details':{'agent_host_concurrency':result['details']}}


def observability(metrics_database_url_file,rules_file):
    """NX-030: the alert evaluator ran in the last five minutes for each active tenant's real world, with the rule version of
    the file; process samples are being written. Read through the read-only nexloop_metrics role only (D4); backups are reported
    (unavailable until NX-035) but never fail the check."""
    from nexloop_eios.observability import load_rules,read_export
    try:
        version=load_rules(rules_file)['version']
    except Exception:
        return {'checks':{'alert_rules_file':False},'details':{'observability_error':'alert rules file invalid'}}
    try:
        export=read_export(metrics_database_url_file)
    except Exception:
        return {'checks':{'alert_rules_file':True,'metrics_export':False},'details':{'observability_error':'metrics export unavailable'}}
    real=[snapshot for snapshot in export if snapshot['world']=='real']
    evaluators={s['tenant_id']:s.get('evaluator') or {} for s in real}
    checks={'alert_rules_file':True,'metrics_export':True,
        'alert_evaluator_recent':bool(real) and all(e.get('stale') is False for e in evaluators.values()),
        'alert_rules_current':bool(real) and all(e.get('rules_version')==version for e in evaluators.values()),
        'process_samples_recent':any((s.get(kind) or {}).get('status')=='ok' for s in real for kind in ('guard','pool','host'))}
    details={'observability':{'rules_file_version':version,'tenants':{t:{'rules_version':e.get('rules_version'),'last_evaluated_at':e.get('last_evaluated_at'),
        'stale':e.get('stale')} for t,e in sorted(evaluators.items())},
        'samples':{kind:sorted({(s.get(kind) or {}).get('status','unavailable') for s in real}) for kind in ('guard','pool','host')},
        'backup':sorted({(s.get('backup') or {}).get('status','unavailable') for s in real})}}
    return {'checks':checks,'details':details}


def main():
    parser=argparse.ArgumentParser(description='Read-only NexLoop foundation doctor')
    parser.add_argument('--artifact-root')
    parser.add_argument('--database-url-file',type=Path)
    parser.add_argument('--mode',choices=['test'])
    parser.add_argument('--connection-budget',type=Path,help='versioned connection budget (deploy/stage/connection-budget.v1.json)')
    parser.add_argument('--budget-only',action='store_true',help='only the connection budget and max_connections checks (pre-start gate)')
    parser.add_argument('--agent-host-config',type=Path,help='private Agent Host runtime configuration (maximum_active_runs <= 4)')
    parser.add_argument('--agent-host-only',action='store_true',help='only the Agent Host concurrency check (Agent Host pre-start gate)')
    parser.add_argument('--observability',action='store_true',help='NX-030: alert evaluator, rule version and process samples (read-only metrics role)')
    parser.add_argument('--metrics-database-url-file',type=Path,help='private DSN of the read-only nexloop_metrics role')
    parser.add_argument('--alert-rules',type=Path,help='deploy/configuration/alert-rules.v<N>.json')
    args=parser.parse_args()
    if args.observability:
        if not args.metrics_database_url_file or not args.alert_rules:parser.error('--observability requires --metrics-database-url-file and --alert-rules')
        result=observability(args.metrics_database_url_file,args.alert_rules)
        result.update(foundation_checks_passed=all(result['checks'].values()),product_ready=False)
        print(json.dumps(result,indent=2,default=str))
        return 0 if result['foundation_checks_passed'] else 1
    if args.budget_only and args.agent_host_only:parser.error('--budget-only and --agent-host-only are separate pre-start gates')
    if args.agent_host_only:
        if not args.agent_host_config:parser.error('--agent-host-only requires --agent-host-config')
        result=agent_host_concurrency(args.agent_host_config)
        result.update(foundation_checks_passed=all(result['checks'].values()),product_ready=False)
        print(json.dumps(result,indent=2))
        return 0 if result['foundation_checks_passed'] else 1
    if args.budget_only and not args.connection_budget:parser.error('--budget-only requires --connection-budget')
    if not args.budget_only and not args.artifact_root:parser.error('--artifact-root is required')
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
    if args.budget_only:
        result={'checks':{},'details':{},'product_ready':False}
    else:
        result=diagnose(database_url=database_url,artifact_root=args.artifact_root,
            environment={'MODEL_PROVIDER':'test'} if args.mode=='test' else None)
    if args.connection_budget:
        budget=connection_budget(budget_file=args.connection_budget,database_url=database_url)
        result['checks'].update(budget['checks']);result['details'].update(budget['details'])
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
