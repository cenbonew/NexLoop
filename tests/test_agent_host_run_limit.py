"""ADR-022 §4: the Agent Host's global active-Run limit, with actual Pi, TLS Host, guard and PG.

maximum_active_runs=1 makes the second of two Role Runs the "fifth" Run of a full Host:
it never reaches the guard while the first is active; with no wait budget it is refused with
the explicit retryable runtime_capacity_exhausted, with a wait budget it is admitted only
after the first Run settled. Waiting builds no proof (no guard request is made meanwhile).

The guard runs as 4 child processes, the deployment premise of ADR-022 §4 / ADR-024 (stage
`--guard-workers 4`). An in-process guard shares one GIL with pytest, the Host polling and both
Runs; on the deploy host its requests then exceed the Host's 2 s limit (NX-049), which would
test the guard's capacity instead of the Host's admission limit. Guard calls are observed in the
children through the test-only tests/support/guard_call_log (Run id and operation only).
"""
from contextlib import contextmanager
import json,os,secrets,threading,time
from pathlib import Path
import httpx
from role_run_fixture import role_runtime_plan  # noqa: F401
from runtime_effect_fixture import runtime_effect_plan  # noqa: F401
from test_agent_host import files,trusted_tls
from test_runtime_host_admission import guard_server,host
from test_runtime_effect_tools import effect_configuration

CALL_LOG_HOOK=Path(__file__).resolve().parent/'support'/'guard_call_log'
GUARD_PROCESSES=4  # deployment premise (ADR-022 §4, deploy/stage --guard-workers 4)


@contextmanager
def observed_guard(plan,tmp_path,guard_key):
    """4 guard child processes that append their calls to a log; the environment is set only
    while the children start, so the Host and later subprocesses do not load the hook."""
    log=tmp_path/'guard-calls.jsonl'
    saved={name:os.environ.get(name) for name in ('PYTHONPATH','NEXLOOP_TEST_GUARD_CALL_LOG')}
    os.environ['PYTHONPATH']=os.pathsep.join(filter(None,[str(CALL_LOG_HOOK),saved['PYTHONPATH']]))
    os.environ['NEXLOOP_TEST_GUARD_CALL_LOG']=str(log)
    try:
        server=guard_server(plan['worker'],tmp_path,guard_key,spawn=plan['worker_spawn'],workers=GUARD_PROCESSES)
        port=server.__enter__()
    finally:
        for name,value in saved.items():
            if value is None:os.environ.pop(name,None)
            else:os.environ[name]=value
    try:
        def calls():
            if not log.exists():return []
            return [(row['at'],row['run_id'],row['operation']) for row in map(json.loads,log.read_text().splitlines())]
        yield port,calls
    finally:
        server.__exit__(None,None,None)


def setup(tmp_path):
    runtime,key=files(tmp_path)
    guard_key=tmp_path/'guard-key';guard_key.write_text(secrets.token_hex(32));guard_key.chmod(0o600)
    return runtime,key,guard_key


def start_body(plan,index):
    command=plan['commands'][index]
    return {'activation_ref':plan['activations'][index],'command':command,'input':plan['context_packs'][command['run_id']]['input']}


def wait_done(client,headers,plan,index,timeout=60):
    command=plan['commands'][index];deadline=time.monotonic()+timeout
    while True:
        result=client.post('/internal/v1/runs/inspect',headers=headers,json={'activation_ref':plan['activations'][index],'command':command}).json()
        if result.get('submission_status')=='done':return result
        assert time.monotonic()<deadline,result
        time.sleep(.05)


def configured(tmp_path,port,guard_key,wait_ms):
    config=effect_configuration(tmp_path,port,guard_key);body=json.loads(config.read_text());body.pop('deterministic_effect_message')
    body.update(deterministic_message_from_input=True,context_input_protocol='nexloop.context-pack.v5',maximum_active_runs=1,run_admission_wait_ms=wait_ms)
    config.write_text(json.dumps(body));return config


def test_full_host_refuses_a_further_run_with_retryable_error_before_any_guard_request(role_runtime_plan,tmp_path):
    plan=role_runtime_plan;runtime,key,guard_key=setup(tmp_path)
    second=plan['commands'][1]['run_id']
    with observed_guard(plan,tmp_path,guard_key) as (port,calls):
        with host(runtime,key,configured(tmp_path,port,guard_key,0)) as (_,client,headers):
            assert client.post('/internal/v1/runs/start',headers=headers,json=start_body(plan,0)).status_code==202
            refused=client.post('/internal/v1/runs/start',headers=headers,json=start_body(plan,1))
            assert refused.status_code==503 and refused.json()['code']=='runtime_capacity_exhausted' and refused.json()['retryable'] is True
            assert calls() and not any(run==second for _,run,_ in calls()) and not (runtime/second.lower()).exists()  # nothing built, nothing stored
            assert wait_done(client,headers,plan,0)['runtime_outcome']=='succeeded'
            deadline=time.monotonic()+10
            while True:  # the slot is released once the first Run's harness is idle
                retried=client.post('/internal/v1/runs/start',headers=headers,json=start_body(plan,1))
                if retried.status_code==202:break
                assert retried.json()['code']=='runtime_capacity_exhausted' and time.monotonic()<deadline;time.sleep(.05)
            assert wait_done(client,headers,plan,1)['runtime_outcome']=='succeeded'


def test_full_host_queues_a_further_run_until_the_active_one_settles(role_runtime_plan,tmp_path):
    plan=role_runtime_plan;runtime,key,guard_key=setup(tmp_path)
    first_run,second=plan['commands'][0]['run_id'],plan['commands'][1]['run_id']
    with observed_guard(plan,tmp_path,guard_key) as (port,calls):
        with host(runtime,key,configured(tmp_path,port,guard_key,30000)) as (_,client,headers):
            assert client.post('/internal/v1/runs/start',headers=headers,json=start_body(plan,0)).status_code==202
            admitted={}
            def queued():
                with httpx.Client(base_url=client.base_url,verify=trusted_tls(key),trust_env=False,timeout=60) as patient:
                    response=patient.post('/internal/v1/runs/start',headers=headers,json=start_body(plan,1))
                    admitted.update(status=response.status_code,at=time.time())
            thread=threading.Thread(target=queued);thread.start()
            assert wait_done(client,headers,plan,0)['runtime_outcome']=='succeeded'
            thread.join(60);assert admitted['status']==202
            # No overlap of execution: every guard request of the queued Run comes after the last
            # execution request (start/model/tool) of the first one; inspect polling is excluded.
            observed=calls()
            last_first=max(at for at,run,operation in observed if run==first_run and operation in ('start','model','tool'))
            assert min(at for at,run,_ in observed if run==second)>last_first and admitted['at']>last_first
            assert wait_done(client,headers,plan,1)['runtime_outcome']=='succeeded'
