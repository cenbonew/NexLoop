"""ADR-024 / 11_DEPLOYMENT §6: declared PG pools <= 60 and server max_connections >= 80, checked before start."""
import copy,ipaddress,json,os,re,subprocess,sys
from pathlib import Path
import psycopg
import pytest
import yaml
from nexloop_eios import connection_budget as budget
from nexloop_eios.runtime_worker import _arguments,backend_pool_max,guard_pool_max

ROOT=Path(__file__).resolve().parents[1]
STAGE=ROOT/'deploy/stage'
DOCUMENT=json.loads((STAGE/'connection-budget.v1.json').read_text())


def runtime(document):return next(s for s in document['services'] if s['name']=='runtime-worker')


def test_declared_stage_budget_is_within_policy():
    result=budget.evaluate(DOCUMENT,max_connections=80)
    # NX-031 D7 (measured smallest workable pools): api 2x4 + domain-worker 2 + action-worker 2 + runtime 2+4x4
    # + message-relay 2 + outbound-recorder 2 + 11 background services x 1 + 2 launcher API pools x 2 = 53,
    # = 49 without the removed scheduler reserve (dispatcher 2026-10-11), + the API browser identity pools 2 x 4 (NX-032) = 57,
    # + the NX-037 webchat-delivery service 1 x 2 = 59; option B (dispatcher 2026-10-13): message-relay 2 x 2 (+2) and the identity
    # pools 2 x 3 (-2) = 59.
    assert result['passed'] and result['details']['application_pool_total']==59 and result['details']['missing_services']==[]
    assert result['details']['services']['runtime-worker']==2+4*4


def test_dispatch_loops_share_the_dispatcher_pool_and_stay_within_the_host_limit():
    # NX-031: N loops add no pool; processes x dispatch_loops may not exceed the Agent Host deployment limit (ADR-022 §4).
    result=budget.evaluate(DOCUMENT)
    assert result['checks']['runtime_dispatch_loops'] and result['details']['runtime_dispatch_loops']==4==result['details']['host_active_runs_limit']
    for change in ({'dispatch_loops':5},{'processes':2}):
        document=copy.deepcopy(DOCUMENT);runtime(document).update(change)
        over=budget.evaluate(document)
        assert not over['checks']['runtime_dispatch_loops'] and not over['passed']
    document=copy.deepcopy(DOCUMENT);runtime(document)['dispatch_loops']=1
    assert budget.evaluate(document)['details']['services']['runtime-worker']==2+4*4


def test_runtime_worker_single_process_counts_only_the_guard_pool():
    entry=dict(runtime(DOCUMENT),guard_workers=1)
    assert budget.service_connections(entry)==entry['guard_pool_max']


@pytest.mark.parametrize('max_connections,passed',[(80,True),(100,True),(79,False),(None,True)])
def test_max_connections_floor(max_connections,passed):
    result=budget.evaluate(DOCUMENT,max_connections=max_connections)
    assert result['passed'] is passed and ('max_connections' in result['checks'])==(max_connections is not None)


def test_total_over_sixty_fails():
    # 59 at the default four guard workers (NX-032 identity pools, NX-037 webchat-delivery): one more guard process breaks the limit.
    document=copy.deepcopy(DOCUMENT);runtime(document)['guard_workers']=5  # 2 + 5*4 = 22 -> 63 total
    result=budget.evaluate(document)
    assert not result['passed'] and result['details']['application_pool_total']==63


@pytest.mark.parametrize('mutate',[
    lambda d:d.update(schema='nexloop.connection-budget.v0'),
    lambda d:d.update(services=[]),
    lambda d:d['services'].append(dict(d['services'][0])),                     # duplicate name
    lambda d:d['services'][0].update(processes=0),
    lambda d:d['services'][0].update(pool_max='4'),
    lambda d:d['services'][0].update(pool_max=4,extra=1),                      # unexpected field
    lambda d:d['services'][0].update(name='API Server'),
    lambda d:runtime(d).update(guard_pool_max=1),                              # admits no guard request
    lambda d:runtime(d).update(guard_workers=17),
    lambda d:runtime(d).pop('dispatcher_pool_max'),
    lambda d:runtime(d).pop('dispatch_loops'),
    lambda d:runtime(d).update(dispatch_loops=0),
    lambda d:runtime(d).update(dispatch_loops=17),
    lambda d:runtime(d).update(pool_max=4),                                    # mixed shapes
])
def test_invalid_budget_documents_are_refused(mutate):
    document=copy.deepcopy(DOCUMENT);mutate(document)
    with pytest.raises(budget.BudgetInvalid):budget.evaluate(document)


def private(path,value):
    path.write_text(value);path.chmod(0o600);return path


def doctor(*arguments):
    return subprocess.run([sys.executable,'-m','nexloop_eios.doctor',*arguments],capture_output=True,text=True,timeout=60,
        env={k:v for k,v in os.environ.items() if not k.startswith(('DATABASE_','NEXLOOP_DATABASE'))})


def test_doctor_budget_gate_against_real_postgres(pg,tmp_path):
    dsn=private(tmp_path/'dsn',pg)
    passed=doctor('--budget-only','--connection-budget',str(STAGE/'connection-budget.v1.json'),'--database-url-file',str(dsn))
    report=json.loads(passed.stdout)
    assert passed.returncode==0,report
    assert report['checks']=={'connection_budget_file':True,'connection_budget_total':True,'connection_budget_services':True,'runtime_dispatch_loops':True,
                               'max_connections':True,'database_configuration':True,'simulation_connection_budget_total':True,'server_capacity':True}
    assert report['details']['connection_budget']['max_connections']>=80 and pg not in passed.stdout+passed.stderr
    # ADR-037 (L6 review 1.1): the superuser reserve is the server's own value, read by the doctor.
    with psycopg.connect(pg) as c:
        assert report['details']['connection_budget']['superuser_reserved']==int(c.execute('show superuser_reserved_connections').fetchone()[0])
    sim=copy.deepcopy(DOCUMENT);simulation(sim)['sim-runtime-worker']['guard_pool_max']=4
    report=json.loads(doctor('--budget-only','--connection-budget',str(private(tmp_path/'sim.json',json.dumps(sim))),'--database-url-file',str(dsn)).stdout)
    assert report['checks']['simulation_connection_budget_total'] is False and report['checks']['connection_budget_total'] is True
    over=copy.deepcopy(DOCUMENT);runtime(over)['guard_workers']=10
    failed=doctor('--budget-only','--connection-budget',str(private(tmp_path/'over.json',json.dumps(over))),'--database-url-file',str(dsn))
    assert failed.returncode==1 and json.loads(failed.stdout)['checks']['connection_budget_total'] is False
    # NX-031 D7: a long-running service missing from the budget fails the gate even when the sum is within the limit.
    partial=copy.deepcopy(DOCUMENT);partial['services']=[s for s in partial['services'] if s['name']!='outbound-recorder']
    missing=doctor('--budget-only','--connection-budget',str(private(tmp_path/'partial.json',json.dumps(partial))),'--database-url-file',str(dsn))
    report=json.loads(missing.stdout)
    assert missing.returncode==1 and report['checks']['connection_budget_services'] is False and report['checks']['connection_budget_total'] is True
    assert report['details']['connection_budget']['missing_services']==['outbound-recorder']
    # NX-031: more dispatch loops than the Agent Host deployment limit fail the gate (no extra connection is counted).
    loops=copy.deepcopy(DOCUMENT);runtime(loops)['dispatch_loops']=5
    report=json.loads(doctor('--budget-only','--connection-budget',str(private(tmp_path/'loops.json',json.dumps(loops))),'--database-url-file',str(dsn)).stdout)
    assert report['checks']['runtime_dispatch_loops'] is False and report['checks']['connection_budget_total'] is True
    broken=doctor('--budget-only','--connection-budget',str(private(tmp_path/'broken.json','{')),'--database-url-file',str(dsn))
    assert broken.returncode==1 and json.loads(broken.stdout)['checks']['connection_budget_file'] is False


def test_doctor_budget_gate_fails_without_reachable_database(tmp_path):
    dsn=private(tmp_path/'dsn','host=/nonexistent-nexloop-socket dbname=none user=none connect_timeout=1')
    result=doctor('--budget-only','--connection-budget',str(STAGE/'connection-budget.v1.json'),'--database-url-file',str(dsn))
    assert result.returncode==1 and json.loads(result.stdout)['checks']['max_connections'] is False
    assert doctor('--budget-only').returncode==2  # the gate needs a budget file


def compose_runtime():
    return yaml.safe_load((STAGE/'compose.runtime-worker.example.yaml').read_text())['services']


def flag(command,name):return str(command[command.index(name)+1])


def test_stage_examples_match_the_declared_budget():
    entry=runtime(DOCUMENT)
    services=compose_runtime();worker=services['runtime-worker']
    assert flag(worker['command'],'--guard-workers')==str(entry['guard_workers'])==str(4)
    assert flag(worker['command'],'--dispatcher-pool-max')==str(entry['dispatcher_pool_max'])
    assert worker['environment']['NEX_EIOS_DB_POOL_MAX']==str(entry['guard_pool_max'])
    assert worker['depends_on']=={'runtime-worker-budget':{'condition':'service_completed_successfully'}}
    assert services['runtime-worker-budget']['command'][:3]==['--budget-only','--connection-budget','/etc/nexloop/connection-budget.v1.json']
    unit=(STAGE/'systemd/nexloop-runtime-worker.service.example').read_text()
    assert f"--guard-workers {entry['guard_workers']} " in unit and f"--dispatcher-pool-max {entry['dispatcher_pool_max']} " in unit
    assert flag(worker['command'],'--dispatch-loops')==str(entry['dispatch_loops'])==str(4) and f"--dispatch-loops {entry['dispatch_loops']}\n" in unit
    assert f"Environment=NEX_EIOS_DB_POOL_MAX={entry['guard_pool_max']}" in unit
    assert re.search(r'^ExecStartPre=\S*nexloop-doctor --budget-only --connection-budget ',unit,re.M)
    assert 'KillMode=mixed' in unit and 'TimeoutStopSec=40' in unit and worker['stop_grace_period']=='40s'


DOCUMENTATION_RANGES=('192.0.2.0/24','198.51.100.0/24','203.0.113.0/24')  # RFC 5737


def example_address_allowed(candidate):
    """In stage examples only loopback, the unspecified address (0.0.0.0 in reject rules and container binds) and the RFC 5737
    documentation ranges may appear; private (RFC 1918), shared (100.64/10) and public addresses never do."""
    address=ipaddress.ip_address(candidate)
    return address.is_loopback or address.is_unspecified or any(address in ipaddress.ip_network(n) for n in DOCUMENTATION_RANGES)


def test_stage_examples_contain_no_real_addresses():
    for path in STAGE.rglob('*'):
        if not path.is_file():continue
        text=path.read_text()
        for candidate in re.findall(r'\b\d{1,3}(?:\.\d{1,3}){3}\b',text):
            assert example_address_allowed(candidate),f'{path.name}: {candidate}'
        assert not re.search(r'\b[a-z0-9-]+\.(?:lan|local|internal|home)\b',text),path.name


def test_the_address_rule_still_refuses_private_shared_and_public_addresses():
    # Built from integers so that no private or shared address literal is tracked (the publication scan refuses them).
    def dotted(a,b,c,d):return str(ipaddress.ip_address((a<<24)|(b<<16)|(c<<8)|d))
    refused_addresses=(dotted(10,0,0,1),dotted(10,255,255,254),dotted(172,16,0,1),dotted(172,31,250,7),dotted(192,168,1,1),
                       dotted(100,64,0,1),dotted(100,127,255,254),dotted(8,8,8,8),dotted(1,1,1,1),
                       dotted(192,0,3,1),dotted(198,51,101,1),dotted(203,0,114,1))
    for refused in refused_addresses:
        assert not example_address_allowed(refused),refused
    for allowed in ('127.0.0.1','0.0.0.0','192.0.2.10','198.51.100.200','203.0.113.5'):
        assert example_address_allowed(allowed),allowed


def test_runtime_worker_pool_sizes(monkeypatch,tmp_path):
    base=['--database-url-file','d','--signing-key-file','s','--service-credential-file','c','--artifact-root','a',
        '--host-control-key-file','k','--host-ca-file','ca','--guard-key-file','g','--guard-certificate-file','gc','--guard-tls-key-file','gt',
        '--world','real','--queue','operations','--host-origin','https://127.0.0.1:1','--guard-port','2000']
    monkeypatch.delenv('NEX_EIOS_DB_POOL_MAX',raising=False)
    single=_arguments(base)
    assert (single.guard_workers,single.guard_pool_max,backend_pool_max(single))==(1,4,4)  # unchanged default
    multi=_arguments(base+['--guard-workers','4'])
    assert (multi.dispatcher_pool_max,multi.guard_pool_max,backend_pool_max(multi))==(2,4,2)
    monkeypatch.setenv('NEX_EIOS_DB_POOL_MAX','6')
    assert backend_pool_max(_arguments(base))==6 and _arguments(base+['--guard-workers','4']).guard_pool_max==6
    for value in ('1','33','x',''):
        assert pytest.raises(ValueError,guard_pool_max,{'NEX_EIOS_DB_POOL_MAX':value})
        monkeypatch.setenv('NEX_EIOS_DB_POOL_MAX',value)
        with pytest.raises(SystemExit):_arguments(base)
    monkeypatch.delenv('NEX_EIOS_DB_POOL_MAX')
    for value in ('0','33'):
        with pytest.raises(SystemExit):_arguments(base+['--dispatcher-pool-max',value])
    assert single.dispatch_loops==4 and _arguments(base+['--dispatch-loops','1']).dispatch_loops==1  # NX-031 default = Host default
    for value in ('0','17','x'):
        with pytest.raises(SystemExit):_arguments(base+['--dispatch-loops',value])


def test_every_background_service_is_budgeted_with_the_pool_it_opens():
    """NX-031 D7: each background service process (and the two launchers' API pools) is declared with exactly the pool size
    the code opens by default, so the doctor's sum is the real ceiling."""
    from nexloop_eios import background_services as B
    declared={s['name']:s for s in DOCUMENT['services']}
    for service in B.SERVICES:
        assert declared[service]=={'name':service,'processes':1,'pool_max':B.POOL_MAX},service
        assert B._parser(service).parse_args(['--database-url-file','x','--signing-key-file','x','--service-credential-file','x',
            '--artifact-root','x','--world','real',*_required(service)]).pool_max==B.POOL_MAX
    assert set(budget.required_services())=={s['name'] for s in DOCUMENT['services']}
    for service in ('plan-reevaluator','reply-guarantor'):
        assert declared[service+'-launcher']=={'name':service+'-launcher','processes':1,'pool_max':B.API_POOL_MAX}
        assert B._parser(service).parse_args(['--database-url-file','x','--signing-key-file','x','--service-credential-file','x','--artifact-root','x',
            '--world','real',*_required(service)]).api_pool_max==B.API_POOL_MAX
    assert (B.POOL_MAX,B.API_POOL_MAX)==(1,2)
    from nexloop_eios import effect_worker,message_relay_cli,outbound_messages
    for name,module in (('action-worker',effect_worker),('message-relay',message_relay_cli),('outbound-recorder',outbound_messages)):
        # Two relays (dispatcher 2026-10-13, budget option B: each with its own vault in the stage Compose).
        assert declared[name]=={'name':name,'processes':2 if name=='message-relay' else 1,'pool_max':2} and module.POOL_MAX==2,name
    from nexloop_eios import domain_worker
    assert declared['domain-worker']['pool_max']==domain_worker.POOL_MAX==2


def _required(service):
    from nexloop_eios import background_services as B
    extra={'claim-extraction-worker':['--model-env-file','x'],'claim-matcher':['--model-env-file','x','--match-config-file','x'],
        'recall-indexer':['--types-file','x'],'commitment-keeper':['--settings-file','x'],'commercial-recorder':['--settings-file','x'],
        'plan-reevaluator':['--settings-file','x','--api-database-url-file','x','--effect-action','eios:action:x:1',
            *[a for n in B.LAUNCH_CREDENTIALS for a in (f'--{n}-credential-file','x')]],
        'incentive-issuer':['--ledger-directory','x'],
        'reply-guarantor':['--policy-file','x','--api-database-url-file','x','--recipe-file','x',*[a for n in B.RELAY_CREDENTIALS for a in (f'--{n}-credential-file','x')]]}
    return extra.get(service,[])


def test_a_missing_long_running_service_fails_the_budget():
    """NX-031 D7: the sum alone is not enough; every long-running service must be declared."""
    for name in budget.required_services():
        document=copy.deepcopy(DOCUMENT);document['services']=[s for s in document['services'] if s['name']!=name]
        result=budget.evaluate(document)
        assert not result['passed'] and result['checks']=={'application_pool_total':True,'all_services_declared':False,'runtime_dispatch_loops':True,'simulation_pool_total':True}
        assert result['details']['missing_services']==[name]


# --- ADR-037: the simulation group's own budget (owner 2026-10-22) -------------------------------------------------------------------

def simulation(document):return {s['name']:s for s in document['simulation']['services']}


def test_the_application_and_simulation_budgets_are_summed_apart():
    result=budget.evaluate(DOCUMENT,max_connections=80,superuser_reserved=3)
    assert result['passed'] and result['details']['application_pool_total']==59 and result['details']['simulation_pool_total']==8
    assert result['details']['simulation_services']=={'sim-runtime-worker':6,'sim-stand-in-executor':1,'sim-world-files':1}
    assert 'sim-runtime-worker' not in result['details']['services']  # never in the application's 60
    assert result['checks']['simulation_pool_total'] and result['checks']['server_capacity']  # 59 + 8 + 7 <= 80 - 3
    assert (budget.APPLICATION_POOL_LIMIT,budget.SIMULATION_POOL_LIMIT,budget.OPERATIONS_RESERVE)==(60,10,7)
    # The capacity uses the server's own superuser reserve: a larger reserve on a minimal server does not fit at the limits.
    assert budget.evaluate(DOCUMENT,max_connections=80,superuser_reserved=7)['checks']['server_capacity'] is False  # 59 + 8 + 7 = 74 > 80 - 7
    assert 'server_capacity' not in budget.evaluate(DOCUMENT,max_connections=80)['checks']  # the doctor always gives both



def test_a_simulation_group_over_ten_fails():
    document=copy.deepcopy(DOCUMENT);simulation(document)['sim-runtime-worker']['guard_pool_max']=4  # 2 + 2*4 = 10 -> 12 total
    result=budget.evaluate(document)
    assert not result['passed'] and result['details']['simulation_pool_total']==12 and result['checks']['simulation_pool_total'] is False
    assert result['checks']['application_pool_total']  # the application's sum is unchanged


@pytest.mark.parametrize('mutate',[
    lambda d:d.pop('simulation'),
    lambda d:d['simulation']['services'].append({'name':'runtime-worker-x','processes':1,'pool_max':1}),   # not sim-
    lambda d:d['simulation']['services'].append({'name':'api','processes':1,'pool_max':1}),                # an application name
    lambda d:d['simulation'].update(extra=1),
    lambda d:d.update(extra=1),
])
def test_invalid_simulation_sections_are_refused(mutate):
    document=copy.deepcopy(DOCUMENT);mutate(document)
    with pytest.raises(budget.BudgetInvalid):budget.evaluate(document)


def test_the_simulation_compose_matches_its_budget_both_ways():
    from nexloop_eios.deploy_checks import simulation_compose_budget
    compose=yaml.safe_load((STAGE/'compose.simulation.example.yaml').read_text())
    result=simulation_compose_budget(DOCUMENT,compose)
    assert result['checks']['simulation_compose_matches_budget'],result
    worker=compose['services']['sim-runtime-worker']
    assert worker['environment']['NEX_EIOS_DB_POOL_MAX']=='2' and flag(worker['command'],'--guard-workers')=='2' and flag(worker['command'],'--dispatcher-pool-max')=='2'
    assert flag(worker['command'],'--dispatch-loops')=='2'==str(simulation(DOCUMENT)['sim-runtime-worker']['dispatch_loops'])  # L6 review 4.2
    assert compose['services']['sim-agent-host']['labels']['nexloop.budget-service']=='none'  # opens no PostgreSQL connection
    pools=copy.deepcopy(compose);pools['services']['sim-runtime-worker']['environment']['NEX_EIOS_DB_POOL_MAX']='4'
    assert simulation_compose_budget(DOCUMENT,pools)['details']['simulation_compose_budget']==['simulation_runtime_pools_differ:sim-runtime-worker']
    unlabelled=copy.deepcopy(compose);unlabelled['services']['sim-world-files']['labels']={'nexloop.budget-service':'none'}
    assert 'budget_entry_without_service:sim-world-files' in simulation_compose_budget(DOCUMENT,unlabelled)['details']['simulation_compose_budget']
    extra=copy.deepcopy(DOCUMENT);extra['simulation']['services'].append({'name':'sim-extra','processes':1,'pool_max':1})
    assert 'budget_entry_without_service:sim-extra' in simulation_compose_budget(extra,compose)['details']['simulation_compose_budget']


def test_an_environment_without_the_simulation_group_declares_an_empty_section():
    """L6 review 5.1: {"services": []} is valid (0 connections), so a real deployment without the group is never refused for it."""
    document=copy.deepcopy(DOCUMENT);document['simulation']={'services':[]}
    result=budget.evaluate(document,max_connections=80,superuser_reserved=3)
    assert result['passed'] and result['details']['simulation_pool_total']==0 and result['details']['simulation_services']=={}


def test_the_deploy_gate_fails_when_the_simulation_project_runs_without_its_compose(tmp_path):
    """L6 review 2.1: the inventory lists nexloop-simulation as running and no simulation Compose is given: the gate fails."""
    from nexloop_eios.deploy_checks import deploy_gate
    budget_file=tmp_path/'budget.json';budget_file.write_text(json.dumps(DOCUMENT))
    def run(projects,**extra):
        after=tmp_path/'after.json';after.write_text(json.dumps({'phase':'after','compose_projects':projects}))
        return deploy_gate({'host_role':'app','connection_budget':str(budget_file),'inventory_after':str(after),**extra})
    assert run({'nexloop-app':{},'nexloop-simulation':{}})['details']['simulation_compose_budget']==['simulation_running_without_compose']
    assert run({'nexloop-app':{},'nexloop-simulation':{}})['checks']['simulation_compose_matches_budget'] is False
    assert 'simulation_compose_matches_budget' not in run({'nexloop-app':{}})['checks']  # not running, not given: nothing to check
    compose=tmp_path/'sim.json';compose.write_text(json.dumps(yaml.safe_load((STAGE/'compose.simulation.example.yaml').read_text())))
    assert run({'nexloop-simulation':{}},simulation_compose_json=str(compose))['checks']['simulation_compose_matches_budget'] is True
