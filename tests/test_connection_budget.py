"""ADR-024 / 11_DEPLOYMENT §6: declared PG pools <= 60 and server max_connections >= 80, checked before start."""
import copy,ipaddress,json,os,re,subprocess,sys
from pathlib import Path
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
    assert result['passed'] and result['details']['application_pool_total']==38
    assert result['details']['services']['runtime-worker']==2+4*4


def test_runtime_worker_single_process_counts_only_the_guard_pool():
    entry=dict(runtime(DOCUMENT),guard_workers=1)
    assert budget.service_connections(entry)==entry['guard_pool_max']


@pytest.mark.parametrize('max_connections,passed',[(80,True),(100,True),(79,False),(None,True)])
def test_max_connections_floor(max_connections,passed):
    result=budget.evaluate(DOCUMENT,max_connections=max_connections)
    assert result['passed'] is passed and ('max_connections' in result['checks'])==(max_connections is not None)


def test_total_over_sixty_fails():
    document=copy.deepcopy(DOCUMENT);runtime(document)['guard_workers']=6  # 2 + 6*4 = 26 -> 46 total, still ok
    assert budget.evaluate(document)['passed']
    runtime(document)['guard_workers']=10  # 2 + 40 -> 62 total
    result=budget.evaluate(document)
    assert not result['passed'] and result['details']['application_pool_total']==62


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
    assert report['checks']=={'connection_budget_file':True,'connection_budget_total':True,'max_connections':True,'database_configuration':True}
    assert report['details']['connection_budget']['max_connections']>=80 and pg not in passed.stdout+passed.stderr
    over=copy.deepcopy(DOCUMENT);runtime(over)['guard_workers']=10
    failed=doctor('--budget-only','--connection-budget',str(private(tmp_path/'over.json',json.dumps(over))),'--database-url-file',str(dsn))
    assert failed.returncode==1 and json.loads(failed.stdout)['checks']['connection_budget_total'] is False
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
    assert f"--guard-workers {entry['guard_workers']} " in unit and f"--dispatcher-pool-max {entry['dispatcher_pool_max']}" in unit
    assert f"Environment=NEX_EIOS_DB_POOL_MAX={entry['guard_pool_max']}" in unit
    assert re.search(r'^ExecStartPre=\S*nexloop-doctor --budget-only --connection-budget ',unit,re.M)
    assert 'KillMode=mixed' in unit and 'TimeoutStopSec=40' in unit and worker['stop_grace_period']=='40s'


def test_stage_examples_contain_no_real_addresses():
    for path in STAGE.rglob('*'):
        if not path.is_file():continue
        text=path.read_text()
        for candidate in re.findall(r'\b\d{1,3}(?:\.\d{1,3}){3}\b',text):
            address=ipaddress.ip_address(candidate)
            assert address.is_loopback,f'{path.name}: {candidate}'
        assert not re.search(r'\b[a-z0-9-]+\.(?:lan|local|internal|home)\b',text),path.name


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
