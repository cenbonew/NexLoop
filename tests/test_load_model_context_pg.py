"""AT-060 design §4.4 item 4 (scripts/load/collect.py): the model request context check compares every ID a model request's
context sources cite with the consumer of that request's own context, for any Run kind (message, fallback, role). Positive
control: a request citing another consumer, that consumer's conversation or message, or a request whose context has no
consumer, is counted as a violation; IDs of the Run's own consumer and non-identity IDs are not. Synthetic rows in a
transaction that is rolled back."""
import hashlib
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from psycopg.types.json import Jsonb

from nexloop_eios.bootstrap import bootstrap
from test_context_v6_pg import v6  # noqa: F401
from test_context_artifacts import context_message  # noqa: F401
from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/load'))
import collect  # noqa: E402

OWN, OTHER = 'a' * 64, 'b' * 64
OWN_CONVERSATION, OTHER_CONVERSATION = 'c' * 64, 'd' * 64
OWN_MESSAGE, OTHER_MESSAGE = 'e' * 64, 'f' * 64
NEUTRAL = '1' * 64  # a PlanStep / Goal style object ID: not a Consumer, Conversation or Message


class Rollback(Exception):
    pass


def model_context(admin, cited_by_request):
    """One model request per entry of `cited_by_request` (None: a request whose context is not an effect context)."""
    tenant, world = collect.TENANT, collect.WORLD
    start = datetime.now(UTC)
    try:
        with admin.transaction():
            admin.execute('set local session_replication_role=replica')  # test harness: rows without their chain
            admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (tenant,))
            for consumer in (OWN, OTHER):
                admin.execute("""insert into ontology.objects(tenant_id,type_name,object_id,schema_version,properties,created_at,updated_at,world,nexloop_revision)
                    values(%s,'Consumer',%s,1,'{}',now(),now(),%s,1)""", (tenant, consumer, world))
            for conversation, consumer, message in ((OWN_CONVERSATION, OWN, OWN_MESSAGE), (OTHER_CONVERSATION, OTHER, OTHER_MESSAGE)):
                admin.execute("insert into runtime.nexloop_conversations(tenant_id,world,conversation_id,consumer_id,principal_id,idempotency_key) values(%s,%s,%s,%s,'p',%s)",
                              (tenant, world, conversation, consumer, 'k' + conversation))
                admin.execute("""insert into runtime.nexloop_conversation_messages(tenant_id,world,conversation_id,sequence,message_id,idempotency_key,payload_digest,record)
                    values(%s,%s,%s,1,%s,%s,%s,%s)""", (tenant, world, conversation, message, 'k' + message, '0' * 64, Jsonb({'accepted_at': start.isoformat()})))
            for n, cited in enumerate(cited_by_request):
                context, run = uuid.uuid4(), uuid.uuid4()
                if cited is not None:
                    admin.execute("""insert into control.nexloop_effect_contexts(context_id,tenant_id,world,consumer_id,goal_identity,goal_version_ref,plan_step_identity,
                        slot_identity,consumer_revision,control_revision,allow_effect,valid_until,budget_units,expected_versions,executor_principal)
                        values(%s,%s,%s,%s,%s,'goal:g@1','step:s','service.request:primary',1,1,true,now()+interval '1 hour',1,'[{}]','executor')""",
                                  (context, tenant, world, OWN, f'goal:{n}'))
                for ordinal, ref in enumerate(cited or ()):
                    h = hashlib.sha256(f'{n}:{ordinal}'.encode()).hexdigest()
                    admin.execute("insert into runtime.nexloop_context_sources values(%s,%s,%s,%s,'evidence',%s,'1',%s,'conversation',%s,'included',null)",
                                  (tenant, world, context, ordinal, ref, h, 'decision:' + h))
                h = '9' * 64
                admin.execute("""insert into runtime.nexloop_model_requests(tenant_id,world,run_id,call_sequence,context_id,model_provider,model_id,tool_manifest_digest,
                    request_digest,prompt_artifact_id,input_token_budget,output_token_budget,settings_digest) values(%s,%s,%s,1,%s,'test','deterministic-test',%s,%s,%s,16000,4000,%s)""",
                              (tenant, world, run, context, h, h, 'c' * 32, h))
            result = collect.checks(admin, start - timedelta(minutes=1), start + timedelta(minutes=10), {'checked': 0, 'violations': 0})['model_context']
            raise Rollback()
    except Rollback:
        pass
    return result


def test_own_consumer_and_non_identity_ids_are_compared_and_pass(admin):
    bootstrap(admin)
    refs = [f'eios:object:Consumer/{OWN}', f'conversation:{OWN_CONVERSATION}', f'message:{OWN_MESSAGE}', f'eios:object:PlanStep/{NEUTRAL}']
    assert model_context(admin, [refs, refs[:2]]) == {'checked': 2, 'ids_compared': 6, 'violations': 0}


def test_positive_control_another_consumer_is_a_violation(admin):
    bootstrap(admin)
    own = [f'eios:object:Consumer/{OWN}', f'message:{OWN_MESSAGE}']
    assert model_context(admin, [own, own + [f'eios:object:Consumer/{OTHER}']]) == {'checked': 2, 'ids_compared': 5, 'violations': 1}
    assert model_context(admin, [own + [f'conversation:{OTHER_CONVERSATION}']])['violations'] == 1
    assert model_context(admin, [own + [f'message:{OTHER_MESSAGE}']])['violations'] == 1
    assert model_context(admin, [own, None]) == {'checked': 2, 'ids_compared': 2, 'violations': 1}  # no consumer: fail closed


def test_a_v6_message_run_through_the_actual_host_is_compared(v6, admin, tmp_path, monkeypatch):  # noqa: F811
    """B1 end to end: a message Run issued by the relay with the deployed strategy (Context v6, as the load cells now do) runs
    on the actual Host; every model request is recorded and the check compares its cited IDs (none foreign). A source citing
    another consumer added to that same context then counts as a violation."""
    import secrets
    from psycopg.conninfo import make_conninfo
    from nexloop_eios.backend import open_backend
    from nexloop_eios.runtime_dispatch import RuntimeDispatcher, HostControlConfiguration
    from test_agent_host import files
    from test_runtime_host_admission import guard_server, configuration
    from test_message_runtime_effect_e2e import actual_host
    f = v6
    o = f['original']
    start = datetime.now(UTC) - timedelta(seconds=1)
    assert f['v6_relay']().run_once() == 'queued'
    home = tmp_path / 'v6-host'
    home.mkdir(mode=0o700)
    runtime, key = files(home)
    guard_key = home / 'v6-guard-key'
    guard_key.write_text(secrets.token_hex(32))
    guard_key.chmod(0o600)
    with open_backend(database_url=make_conninfo(o['pg'], user='nexloop_domain_worker'), artifact_root=home / 'domain-artifacts',
                      signing_key_file=o['paths']['backend_signing'], signing_key_id='explicit-configuration') as backend:
        worker = backend.authenticate(f['f']['tokens']['assembly-runtime-worker'], world='real')
        with guard_server(worker, home, guard_key) as port:
            cfg = configuration(home, port, guard_key)
            body = json.loads(cfg.read_text())
            body.update(effect_tools=True, deterministic_message_from_input=True, context_input_protocol='nexloop.context-pack.v6')
            cfg.write_text(json.dumps(body))
            with actual_host(runtime, key, cfg) as (_, client, headers):
                result = RuntimeDispatcher(worker, HostControlConfiguration(str(client.base_url).rstrip('/'), key, home / 'host-cert.pem'), queue='operations',
                                           lease_seconds=60, total_timeout=45, request_timeout=10).run_once()
                assert result['claimed'] is True and result['status'] == 'succeeded', result.get('result', {}).get('code')
    monkeypatch.setattr(collect, 'TENANT', o['tenant'])
    end = datetime.now(UTC) + timedelta(seconds=1)
    checked = collect.checks(admin, start, end, {'checked': 0, 'violations': 0})['model_context']
    requests = admin.execute('select count(*) from runtime.nexloop_model_requests where tenant_id=%s', (o['tenant'],)).fetchone()[0]
    assert requests >= 1 and checked['checked'] == requests and checked['ids_compared'] > 0 and checked['violations'] == 0, checked
    # Positive control on the real Run's context: cite another consumer (synthetic, rolled back).
    try:
        with admin.transaction():
            admin.execute('set local session_replication_role=replica')
            context, consumer = admin.execute('select q.context_id,e.consumer_id from runtime.nexloop_model_requests q join control.nexloop_effect_contexts e on e.context_id=q.context_id '
                                              'where q.tenant_id=%s limit 1', (o['tenant'],)).fetchone()
            other = 'b' * 64 if consumer != 'b' * 64 else 'a' * 64
            admin.execute("""insert into ontology.objects(tenant_id,type_name,object_id,schema_version,properties,created_at,updated_at,world,nexloop_revision)
                values(%s,'Consumer',%s,1,'{}',now(),now(),'real',1)""", (o['tenant'], other))
            h = '7' * 64
            admin.execute("insert into runtime.nexloop_context_sources values(%s,'real',%s,9999,'evidence',%s,'1',%s,'conversation',%s,'included',null)",
                          (o['tenant'], context, f'eios:object:Consumer/{other}', h, 'decision:' + h))
            planted = collect.checks(admin, start, end, {'checked': 0, 'violations': 0})['model_context']
            raise Rollback()
    except Rollback:
        pass
    assert planted['violations'] >= 1 and planted['ids_compared'] > checked['ids_compared'], planted
