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
        'connection_budget_services':result['checks']['all_services_declared'],'runtime_dispatch_loops':result['checks']['runtime_dispatch_loops'],
        'max_connections':result['checks'].get('max_connections',False)}
    return {'checks':checks,'details':{'connection_budget':result['details']}}


def agent_host_concurrency(config_file):
    """ADR-022 §4: the deployed Agent Host runs at most 4 Runs at once (pre-start gate)."""
    from nexloop_eios import host_concurrency
    try:
        result=host_concurrency.evaluate(host_concurrency.load(read_private_text(config_file,maximum=32768)))
    except Exception as error:
        reason=str(error) if isinstance(error,host_concurrency.HostConcurrencyInvalid) else 'Agent Host configuration is unavailable'
        return {'checks':{'agent_host_concurrency':False,'agent_host_test_only_keys_absent':False},'details':{'agent_host_concurrency_error':reason}}
    return {'checks':{'agent_host_concurrency':result['passed'],'agent_host_test_only_keys_absent':result['test_only_keys_absent']},
        'details':{'agent_host_concurrency':result['details']}}


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


BACKUP_PLAN_KEYS={'schema','version','decision','pg','wal','files','retention','journal','limits','repos'}
def load_backup_plan(path):
    """deploy/configuration/backup.v1.json with bounded validation (NX-035 §3.2); anything else is refused."""
    plan=json.loads(Path(path).read_text())
    limits=plan.get('limits') if type(plan) is dict else None
    if (type(plan) is not dict or set(plan)!=BACKUP_PLAN_KEYS or plan['schema']!='nexloop-backup-plan/1' or type(plan['version']) is not int
            or type(limits) is not dict or set(limits)!={'max_backup_age_seconds','max_full_backup_age_seconds','max_drill_age_seconds'}
            or not all(type(v) is int and 0<v<=60*86400 for v in limits.values())
            or not 0<plan['wal'].get('max_archive_age_seconds',0)<=3600 or not 0<plan['files'].get('max_age_seconds',0)<=86400
            or not 0<plan['journal'].get('max_lag_seconds',0)<=3600 or plan['retention'].get('max_retention_days')!=35
            or plan['repos'].get('required')!=['repo1']):
        raise ValueError('backup plan invalid')
    return plan
def read_backup_status(metrics_database_url_file):
    with psycopg.connect(read_private_text(metrics_database_url_file,maximum=16384),connect_timeout=5) as db:
        if db.execute('select current_user').fetchone()[0]!='nexloop_metrics':raise ValueError('read-only metrics role required')
        return db.execute('select authz.nexloop_backup_status()').fetchone()[0]
def backup(metrics_database_url_file,plan_file):
    """NX-035 §8: the latest backups against the plan, the restore state and the deletion replay, read through the read-only
    nexloop_metrics role (authz.nexloop_backup_status). Missing manifests, a missing plan or anything unknown fail."""
    try:
        plan=load_backup_plan(plan_file)
    except Exception:
        return {'checks':{'backup_plan_file':False},'details':{'backup_error':'backup plan file missing or invalid'}}
    try:
        status=read_backup_status(metrics_database_url_file)
    except Exception:
        return {'checks':{'backup_plan_file':True,'backup_status':False},'details':{'backup_error':'backup status unavailable'}}
    b,limits=status.get('backup') or {},plan['limits']
    def within(key,limit):
        value=b.get(key)
        return type(value) in (int,float) and value<=limit
    checks={'backup_plan_file':True,'backup_status':True,'backup_manifest_present':b.get('status')=='ok',
        'backup_recent':within('age_seconds',limits['max_backup_age_seconds']),
        'full_backup_recent':within('full_backup_age_seconds',limits['max_full_backup_age_seconds']),
        'required_repos_recent':all(type((b.get('repo_backup_age_seconds') or {}).get(r)) in (int,float)
            and b['repo_backup_age_seconds'][r]<=limits['max_backup_age_seconds'] for r in plan['repos']['required']),
        'latest_backup_verified':b.get('verify_failed')==0,
        'wal_archiving':b.get('wal_archive_failing')==0 and within('wal_last_archived_age_seconds',plan['wal']['max_archive_age_seconds']),
        'file_backup_recent':within('file_backup_age_seconds',plan['files']['max_age_seconds']),
        'journal_export':within('journal_export_lag_seconds',plan['journal']['max_lag_seconds']),
        'restore_drill_recent':within('drill_age_seconds',limits['max_drill_age_seconds']),
        'restore_state_open':(status.get('restore') or {}).get('state') in ('open','never_restored'),
        'deletion_replay_closed':status.get('erasure_ready') is True}
    return {'checks':checks,'details':{'backup':{'plan_version':plan['version'],'restore_state':(status.get('restore') or {}).get('state'),
        'failed':sorted(k for k,v in checks.items() if not v)}}}
def real_dispatch(metrics_database_url_file,environment=None):
    """0220: the state of the real dispatch switches, reported only (states and counts): the deployment master switch of this
    process's environment and how many tenants / tenant Actions are open, through the read-only metrics role. A closed state
    passes; only an unreadable status or an invalid master switch value fails."""
    from nexloop_eios.deployment_readiness import dispatch_switch
    deployment=dispatch_switch(os.environ if environment is None else environment)
    try:
        with psycopg.connect(read_private_text(metrics_database_url_file,maximum=16384),connect_timeout=5) as db:
            if db.execute('select current_user').fetchone()[0]!='nexloop_metrics':raise ValueError('read-only metrics role required')
            summary=db.execute('select authz.nexloop_real_dispatch_summary()').fetchone()[0]
    except Exception:
        return {'checks':{'real_dispatch_status':False,'deployment_switch_valid':deployment!='invalid'},'details':{'real_dispatch':{'deployment':deployment}}}
    return {'checks':{'real_dispatch_status':True,'deployment_switch_valid':deployment!='invalid'},
        'details':{'real_dispatch':{'deployment':deployment,'tenants_open':summary.get('tenants_open'),'actions_open':summary.get('actions_open')}}}
RELAY_SOURCE_PRINCIPAL='nexloop-service-relay-source-principal'


def relay_source(metrics_database_url_file):
    """Relay multi-consumer (0161): where a real tenant has enrolled consumers, the relay's Source must be the fixed principal
    nexloop-service-relay-source-principal: an active relay_source_read_rule for it, and an active service credential under it.
    Read through the read-only nexloop_metrics role only."""
    from nexloop_eios.observability import read_export
    try:
        export=read_export(metrics_database_url_file)
    except Exception:
        return {'checks':{'metrics_export':False},'details':{'relay_source_error':'metrics export unavailable'}}
    tenants={}
    for snapshot in export:
        relay=snapshot.get('relay') or {}
        if snapshot.get('world')!='real' or not relay.get('enrolled_consumers'):continue
        rules=[r for r in relay.get('source_rules') or [] if r.get('source_principal')==RELAY_SOURCE_PRINCIPAL]
        tenants[snapshot['tenant_id']]={'enrolled_consumers':relay['enrolled_consumers'],'rule_active':any(r.get('active') for r in rules),
            'credential_active':any(r.get('active') and r.get('credential_active') for r in rules),
            'other_sources':sorted({r.get('source_principal') for r in relay.get('source_rules') or [] if r.get('source_principal')!=RELAY_SOURCE_PRINCIPAL and r.get('active')})}
    checks={'metrics_export':True,'relay_source_rule':all(t['rule_active'] for t in tenants.values()),
        'relay_source_credential':all(t['credential_active'] for t in tenants.values()),
        'relay_source_principal_fixed':all(not t['other_sources'] for t in tenants.values())}
    return {'checks':checks,'details':{'relay_source':{'expected_principal':RELAY_SOURCE_PRINCIPAL,'tenants':dict(sorted(tenants.items()))}}}


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
    parser.add_argument('--deploy-gate',type=Path,help='NX-032 §6: private deploy-gate spec (host_role and input files); every pre-start check of one host')
    parser.add_argument('--tls-spec',type=Path,help='NX-032: private TLS spec (CA, certificates, DSN files, Valkey configuration); names and codes only in the output')
    parser.add_argument('--relay-source',action='store_true',help='relay multi-consumer: the relay Source principal and credential (read-only metrics role)')
    parser.add_argument('--backup',action='store_true',help='NX-035: backups against deploy/configuration/backup.v1.json, restore state (read-only metrics role)')
    parser.add_argument('--backup-plan',type=Path,help='deploy/configuration/backup.v<N>.json')
    parser.add_argument('--real-dispatch',action='store_true',help='0220: real dispatch switch states and counts (read-only metrics role)')
    args=parser.parse_args()
    if args.real_dispatch:
        if not args.metrics_database_url_file:parser.error('--real-dispatch requires --metrics-database-url-file')
        result=real_dispatch(args.metrics_database_url_file)
        result.update(foundation_checks_passed=all(result['checks'].values()),product_ready=False)
        print(json.dumps(result,indent=2,default=str))
        return 0 if result['foundation_checks_passed'] else 1
    if args.backup:
        if not args.metrics_database_url_file or not args.backup_plan:parser.error('--backup requires --metrics-database-url-file and --backup-plan')
        result=backup(args.metrics_database_url_file,args.backup_plan)
        result.update(foundation_checks_passed=all(result['checks'].values()),product_ready=False)
        print(json.dumps(result,indent=2,default=str))
        return 0 if result['foundation_checks_passed'] else 1
    if args.relay_source:
        if not args.metrics_database_url_file:parser.error('--relay-source requires --metrics-database-url-file')
        result=relay_source(args.metrics_database_url_file)
        result.update(foundation_checks_passed=all(result['checks'].values()),product_ready=False)
        print(json.dumps(result,indent=2,default=str))
        return 0 if result['foundation_checks_passed'] else 1
    if args.deploy_gate:
        from nexloop_eios import deploy_checks
        try:
            result=deploy_checks.deploy_gate(json.loads(Path(args.deploy_gate).read_text()))
        except Exception:
            result={'checks':{'deploy_gate_spec':False},'details':{'deploy_gate_error':'deploy gate spec unreadable or invalid'}}
        result.update(foundation_checks_passed=bool(result['checks']) and all(result['checks'].values()),product_ready=False)
        print(json.dumps(result,indent=2,default=str))
        return 0 if result['foundation_checks_passed'] else 1
    if args.tls_spec:
        from nexloop_eios import deploy_checks
        try:
            result=deploy_checks.tls(json.loads(Path(args.tls_spec).read_text()))
        except Exception:
            result={'checks':{'tls_spec':False},'details':{'tls_error':'TLS spec unreadable or invalid'}}
        result.update(foundation_checks_passed=all(result['checks'].values()),product_ready=False)
        print(json.dumps(result,indent=2,default=str))
        return 0 if result['foundation_checks_passed'] else 1
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
