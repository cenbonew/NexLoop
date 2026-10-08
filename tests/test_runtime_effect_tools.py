"""Actual frozen Pi Tool→TLS→restricted EIOS PG admission, no auth callback.

Deterministic Generation profile exercises two distinct tool_call_id values;
no actual model/provider/channel fulfillment is asserted by these tool receipts.
"""
import json,secrets,sqlite3
from nexloop_eios.effect_intents import EffectIntentUnavailable
from runtime_effect_fixture import runtime_effect_plan
from test_agent_host import files
from test_runtime_host_admission import guard_server,host,configuration,completed


def effect_configuration(tmp_path,port,key):
    path=configuration(tmp_path,port,key)
    body=json.loads(path.read_text());body.update(effect_tools=True,deterministic_effect_message='one governed runtime service')
    path.write_text(json.dumps(body));path.chmod(0o600);return path


def tool_evidence(database):
    with sqlite3.connect(f'file:{database}?mode=ro',uri=True) as connection:
        records=[json.loads(row[0]) for row in connection.execute('select record from entries order by id')]
    calls=[];receipts=[]
    for record in records:
        for message in record.get('model',[]):
            if message.get('role')=='assistant':
                calls.extend(item for item in message.get('content',[]) if item.get('type')=='toolCall')
            if message.get('role')=='toolResult':
                assert message.get('isError') is False
                for content in message.get('content',[]):
                    if content.get('type')=='text' and content.get('text','').startswith('{'):
                        receipts.append(json.loads(content['text']))
    return calls,receipts


def test_actual_pi_rebuilt_tool_call_hits_original_business_receipt(runtime_effect_plan,admin,tmp_path):
    plan=runtime_effect_plan;command=plan['commands'][0];ref=plan['activations'][0]
    runtime,key=files(tmp_path);guard_key=tmp_path/'guard-key';guard_key.write_text(secrets.token_hex(32));guard_key.chmod(0o600)
    with guard_server(plan['worker'],tmp_path,guard_key) as port:
        with host(runtime,key,effect_configuration(tmp_path,port,guard_key)) as (_,client,headers):
            accepted=client.post('/internal/v1/runs/start',headers=headers,json={'activation_ref':ref,'command':command,'input':'persist one service intent'})
            assert accepted.status_code==202
            result=completed(client,headers,ref,command)
            assert result['runtime_outcome']=='succeeded' and result['persistence']=={'journal_mode':'wal','synchronous':2}
            calls,receipts=tool_evidence(runtime/command['run_id']/'runtime.sqlite')
            request_calls=[call for call in calls if call['name']=='nexloop.service.request']
            assert [call['id'] for call in request_calls]==['service-request-first','service-request-rebuilt']
            assert len(receipts)==3 and receipts[0]==receipts[1]==receipts[2]
            assert receipts[0]['state']=='accepted' and receipts[0]['scope']=='effect_intent' and receipts[0]['business_action_success'] is False
            assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()[0]==1
            assert admin.execute('select count(*) from runtime.nexloop_effect_outbox').fetchone()[0]==1
            assert admin.execute('select reserved_units from control.nexloop_effect_control_ledger').fetchone()[0]==1
            assert admin.execute('select count(*) from runtime.nexloop_effect_submissions').fetchone()[0]==1
            assert admin.execute('select count(*) from runtime.nexloop_effect_attempts').fetchone()[0]==0
    for path in runtime.rglob('*'):
        if path.is_file():
            contents=path.read_bytes()
            assert all(run.token.encode() not in contents for run in plan['runs'])
            assert guard_key.read_bytes() not in contents
