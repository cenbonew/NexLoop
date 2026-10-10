"""ADR-022 §4: the Agent Host's global active-Run limit, with actual Pi, TLS Host, guard and PG.

maximum_active_runs=1 makes the second of two Role Runs the "fifth" Run of a full Host:
it never reaches the guard while the first is active; with no wait budget it is refused with
the explicit retryable runtime_capacity_exhausted, with a wait budget it is admitted only
after the first Run settled. Waiting builds no proof (no guard request is made meanwhile).
"""
import json,secrets,threading,time
import httpx
from role_run_fixture import role_runtime_plan  # noqa: F401
from runtime_effect_fixture import runtime_effect_plan  # noqa: F401
from test_agent_host import files,trusted_tls
from test_runtime_host_admission import guard_server,host
from test_runtime_effect_tools import effect_configuration


def limited_host(plan,tmp_path,monkeypatch,*,wait_ms):
    from nexloop_eios.backend import AuthenticatedServices
    calls=[];original=AuthenticatedServices.authorize_runtime_activation
    def observed(self,**kwargs):
        calls.append((time.monotonic(),kwargs['command']['run_id'],kwargs['operation']));return original(self,**kwargs)
    monkeypatch.setattr(AuthenticatedServices,'authorize_runtime_activation',observed)
    runtime,key=files(tmp_path)
    guard_key=tmp_path/'guard-key';guard_key.write_text(secrets.token_hex(32));guard_key.chmod(0o600)
    return runtime,key,guard_key,calls


def start_body(plan,index):
    command=plan['commands'][index]
    return {'activation_ref':plan['activations'][index],'command':command,'input':plan['context_packs'][command['run_id']]['input']}


def wait_done(client,headers,plan,index,timeout=60):
    command=plan['commands'][index];deadline=time.monotonic()+timeout
    while True:
        result=client.post('/internal/v1/runs/inspect',headers=headers,json={'activation_ref':plan['activations'][index],'command':command}).json()
        if result.get('submission_status')=='done':return time.monotonic(),result
        assert time.monotonic()<deadline,result
        time.sleep(.05)


def configured(tmp_path,port,guard_key,wait_ms):
    config=effect_configuration(tmp_path,port,guard_key);body=json.loads(config.read_text());body.pop('deterministic_effect_message')
    body.update(deterministic_message_from_input=True,context_input_protocol='nexloop.context-pack.v5',maximum_active_runs=1,run_admission_wait_ms=wait_ms)
    config.write_text(json.dumps(body));return config


def test_full_host_refuses_a_further_run_with_retryable_error_before_any_guard_request(role_runtime_plan,tmp_path,monkeypatch):
    plan=role_runtime_plan;runtime,key,guard_key,calls=limited_host(plan,tmp_path,monkeypatch,wait_ms=0)
    second=plan['commands'][1]['run_id']
    with guard_server(plan['worker'],tmp_path,guard_key) as port:
        with host(runtime,key,configured(tmp_path,port,guard_key,0)) as (_,client,headers):
            assert client.post('/internal/v1/runs/start',headers=headers,json=start_body(plan,0)).status_code==202
            refused=client.post('/internal/v1/runs/start',headers=headers,json=start_body(plan,1))
            assert refused.status_code==503 and refused.json()['code']=='runtime_capacity_exhausted' and refused.json()['retryable'] is True
            assert not any(run==second for _,run,_ in calls) and not (runtime/second.lower()).exists()  # nothing built, nothing stored
            _,first=wait_done(client,headers,plan,0);assert first['runtime_outcome']=='succeeded'
            deadline=time.monotonic()+10
            while True:  # the slot is released once the first Run's harness is idle
                retried=client.post('/internal/v1/runs/start',headers=headers,json=start_body(plan,1))
                if retried.status_code==202:break
                assert retried.json()['code']=='runtime_capacity_exhausted' and time.monotonic()<deadline;time.sleep(.05)
            _,result=wait_done(client,headers,plan,1);assert result['runtime_outcome']=='succeeded'


def test_full_host_queues_a_further_run_until_the_active_one_settles(role_runtime_plan,tmp_path,monkeypatch):
    plan=role_runtime_plan;runtime,key,guard_key,calls=limited_host(plan,tmp_path,monkeypatch,wait_ms=30000)
    second=plan['commands'][1]['run_id']
    with guard_server(plan['worker'],tmp_path,guard_key) as port:
        with host(runtime,key,configured(tmp_path,port,guard_key,30000)) as (_,client,headers):
            assert client.post('/internal/v1/runs/start',headers=headers,json=start_body(plan,0)).status_code==202
            admitted={}
            def queued():
                with httpx.Client(base_url=client.base_url,verify=trusted_tls(key),trust_env=False,timeout=60) as patient:
                    response=patient.post('/internal/v1/runs/start',headers=headers,json=start_body(plan,1))
                    admitted.update(status=response.status_code,at=time.monotonic())
            thread=threading.Thread(target=queued);thread.start()
            done_at,first=wait_done(client,headers,plan,0);assert first['runtime_outcome']=='succeeded'
            thread.join(60);assert admitted['status']==202
            # No overlap of execution: every guard request of the queued Run comes after the last
            # execution request (start/model/tool) of the first one; inspect polling is excluded.
            first_run=plan['commands'][0]['run_id']
            last_first=max(at for at,run,operation in calls if run==first_run and operation in ('start','model','tool'))
            assert min(at for at,run,_ in calls if run==second)>last_first and admitted['at']>last_first
            _,result=wait_done(client,headers,plan,1);assert result['runtime_outcome']=='succeeded'
