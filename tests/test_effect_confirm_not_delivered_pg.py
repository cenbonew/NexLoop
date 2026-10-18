"""0210 (owner 2026-10-18): the owner confirms an unknown effect did not happen. Production create_app, the workbench owner's
HTTP entry and read-only precheck, members applied by the real configurator; the effect ledger rows (attempt, unknown event,
provider observations) are written as the effect worker leaves them. Synthetic data only.

Checked: owner only; the precheck and the refusal name one fixed code; evidence is the latest not_found observation of the
current attempt (no accepted/fulfilled one); wait 24 h after the first unknown result and 10 min after the not_found; the
provider retention must be confirmed; a leased outbox is busy. Then intent and outbox failed, the outbound reply failed, one
determination (reason kept, never shown), nothing sent, unknown_over_24h no longer counts it, a replay answers, a second
determination and an owner requery order are refused. Reply follow-up: owner_follow_up escalates the inbound message; a
fallback reply is allowed only for a message not yet escalated."""
import secrets
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from nexloop_eios.browser_http import BrowserConfiguration
from nexloop_eios.http_api import ApiConfiguration, create_app
from nexloop_eios.workbench_roles import apply_members, compile_members
from commercial_fixture import publish_registry_actions
from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_http import ORIGIN
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_erased_contact_keys_pg import private
from test_workbench_actions_http import act
from test_workbench_read_pg import CALLER, ROLES, WORKBENCH_APP, add_human, members_file, workbench_login, write_facts

PLAN = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)
ACTION = 'nexloop.effect.confirm_not_delivered'


@pytest.fixture
def owner_desk(commitments, identity, uow, admin, pg, tmp_path):
    c = commitments
    publish_registry_actions(admin, (ACTION,))
    admin.execute('insert into control.nexloop_browser_applications values(%s,%s,1,true)', (TENANT, WORKBENCH_APP))
    admin.execute('insert into control.nexloop_browser_business_applications values(%s,%s,%s,%s,%s)', (TENANT, WORKBENCH_APP, CALLER, '1', Jsonb(['action.execute'])))
    admin.execute("insert into control.nexloop_browser_rate_policies values(%s,'local_login','nexloop_identity',50,60,true) on conflict do nothing", (TENANT,))
    owner, operator = add_human(admin, 'owner'), add_human(admin, 'operator')
    admin.execute('alter role nexloop_configurator login')
    members = members_file((owner, 'owner'), (operator, 'operator'))
    apply_members(ROLES, members, database_url_file=private(tmp_path, 'configurator-dsn', make_conninfo(pg, user='nexloop_configurator')))
    write_facts(admin, compile_members(ROLES, members))
    signer = c['signer']
    (tmp_path / 'app-artifacts').mkdir(mode=0o700)
    config = ApiConfiguration(private(tmp_path, 'business-dsn', make_conninfo(pg, user='nexloop_api')), private(tmp_path, 'authority-key', signer.material),
        tmp_path / 'app-artifacts', signer.key_id, BrowserConfiguration(private(tmp_path, 'identity-dsn', identity[0]), private(tmp_path, 'rate-key', secrets.token_hex(32)),
        TENANT, 'synthetic-browser-app', ORIGIN), execution_profile='deterministic-test',
        workbench=BrowserConfiguration(tmp_path / 'identity-dsn', tmp_path / 'rate-key', TENANT, WORKBENCH_APP, ORIGIN))

    class Desk:
        def __init__(self, member):
            self.client = TestClient(create_app(config), base_url=ORIGIN)
            self.client.__enter__()
            self.csrf = workbench_login(self.client, member).json()['csrf_token']

        def precheck(self, intent):
            return self.client.get('/api/v1/workbench/effect-determination/' + intent, headers={'Origin': ORIGIN})

        def confirm(self, intent, *, key=None, **override):
            body = {'intent_id': intent, 'confirmation_ref': 'owner-confirm:1', 'reason': '供应商记录保留期已核对，查询为 not_found',
                    'provider_retention_checked': True, 'reply_followup': 'owner_follow_up', **override}
            return act(self.client, self.csrf, 'confirm_effect_not_delivered', body, key=key)

    desks = []

    def desk(member):
        d = Desk(member);desks.append(d);return d
    c.update(owner=lambda: desk(owner), operator=lambda: desk(operator), owner_member=owner)
    yield c
    for d in desks:
        d.client.__exit__(None, None, None)


def unknown_intent(c, admin, *, unknown_hours=48):
    """An accepted intent of the fixture, then dispatched with an unknown result (the effect worker's ledger rows)."""
    intent = c.submit({'service': 'synthetic service ' + secrets.token_hex(4)})['intent_id']
    tenant, executor = admin.execute('select tenant_id,executor_principal from runtime.nexloop_effect_intents where intent_id=%s', (intent,)).fetchone()
    origin = admin.execute('select run_id from runtime.nexloop_effect_submissions where intent_id=%s order by created_at limit 1', (intent,)).fetchone()[0]
    admin.execute("update runtime.nexloop_effect_intents set state='unknown' where intent_id=%s", (intent,))
    admin.execute("update runtime.nexloop_effect_outbox set state='unknown',available_at='infinity' where intent_id=%s", (intent,))
    admin.execute("insert into runtime.nexloop_effect_attempts(intent_id,attempt_id,tenant_id,world,provider_key,provider_payload_digest,executor_principal,origin_run_id,"
                  "action_claim_revision,action_fencing_token,effect_fence,attempt_revision,state,dispatched_at,updated_at) "
                  "values(%s,gen_random_uuid(),%s,'real',%s,%s,%s,%s,1,%s,1,2,'unknown',clock_timestamp()-make_interval(hours=>%s),clock_timestamp()-make_interval(hours=>%s))",
                  (intent, tenant, str(intent), 'c' * 64, executor, origin, 'd' * 48, unknown_hours, unknown_hours))
    admin.execute("insert into runtime.nexloop_effect_events values(gen_random_uuid(),%s,%s,'real','unknown',1,clock_timestamp()-make_interval(hours=>%s),'{}')",
                  (intent, tenant, unknown_hours))
    return str(intent), tenant


def observe(admin, intent, tenant, state, minutes_ago):
    digest, ref = ('e' * 64, 'provider-ref-1') if state != 'not_found' else (None, None)
    admin.execute("insert into runtime.nexloop_effect_observations(observation_id,intent_id,tenant_id,world,attempt_revision,effect_fence,observer_principal,"
                  "observer_credential_digest,provider_state,provider_payload_digest,provider_reference,observed_at) "
                  "values(gen_random_uuid(),%s,%s,'real',1,1,'synthetic-executor',%s,%s,%s,%s,clock_timestamp()-make_interval(mins=>%s))",
                  (intent, tenant, 'f' * 64, state, digest, ref, minutes_ago))


def states(admin, intent):
    return admin.execute('select i.state,o.state from runtime.nexloop_effect_intents i join runtime.nexloop_effect_outbox o using(intent_id) where intent_id=%s',
                         (intent,)).fetchone()


def refused(response):
    assert response.status_code == 409, response.text
    return response.json()['details']['reason']


@PLAN
def test_the_owner_confirms_not_delivered_only_on_evidence_after_the_wait(owner_desk, admin, governed_effect_executor):
    c = owner_desk;c['executor_fixture'] = governed_effect_executor
    intent, tenant = unknown_intent(c, admin)
    owner, operator = c['owner'](), c['operator']()
    assert operator.precheck(intent).status_code == 403 and operator.confirm(intent).status_code == 403
    assert owner.precheck(str(uuid.uuid4())).json()['code'] == 'intent_unknown'
    # No query result yet.
    assert owner.precheck(intent).json()['code'] == 'no_not_found_evidence' and refused(owner.confirm(intent)) == 'no_not_found_evidence'
    # A fresh not_found: the 10 minute wait.
    observe(admin, intent, tenant, 'not_found', 1)
    check = owner.precheck(intent).json()
    assert check['code'] == 'wait_period' and check['latest_observation']['provider_state'] == 'not_found' and refused(owner.confirm(intent)) == 'wait_period'
    observe(admin, intent, tenant, 'not_found', 11)  # an older one does not count: the latest is still 1 minute old
    assert owner.precheck(intent).json()['code'] == 'wait_period'
    observe(admin, intent, tenant, 'not_found', 0)
    admin.execute("update runtime.nexloop_effect_observations set observed_at=observed_at-interval '20 minutes' where intent_id=%s", (intent,))
    assert owner.precheck(intent).json()['code'] == 'eligible'
    # Input and the retention confirmation.
    assert owner.confirm(intent, reason='').status_code == 422 and owner.confirm(intent, reply_followup='none').status_code == 422
    assert refused(owner.confirm(intent, provider_retention_checked=False)) == 'retention_unconfirmed'
    # The effect worker holds it now: busy.
    admin.execute("update runtime.nexloop_effect_outbox set lease_until=clock_timestamp()+interval '1 minute' where intent_id=%s", (intent,))
    assert refused(owner.confirm(intent)) == 'effect_busy'
    admin.execute('update runtime.nexloop_effect_outbox set lease_until=null where intent_id=%s', (intent,))
    over_24h = "select count(*) from runtime.nexloop_effect_intents where tenant_id=%s and state='unknown' and created_at<clock_timestamp()-interval '24 hours'"
    admin.execute("update runtime.nexloop_effect_intents set created_at=clock_timestamp()-interval '2 days' where intent_id=%s", (intent,))
    before = admin.execute(over_24h, (tenant,)).fetchone()[0]
    attempts = admin.execute('select count(*),max(attempt_revision) from runtime.nexloop_effect_attempts where intent_id=%s', (intent,)).fetchone()
    done = owner.confirm(intent, key='owner-confirm-key-0001')
    assert done.status_code == 200, done.text
    body = done.json()
    assert body['intent_state'] == 'failed' and body['kind'] == 'confirmed_not_delivered' and body['reply_followup'] == 'none'
    assert states(admin, intent) == ('failed', 'failed')
    assert admin.execute(over_24h, (tenant,)).fetchone()[0] == before - 1
    assert admin.execute('select count(*),max(attempt_revision) from runtime.nexloop_effect_attempts where intent_id=%s', (intent,)).fetchone() == attempts  # nothing sent
    row = admin.execute('select kind,confirmation_ref,reason,provider_retention_checked,decided_by is not null from runtime.nexloop_effect_determinations where intent_id=%s',
                        (intent,)).fetchone()
    assert row == ('confirmed_not_delivered', 'owner-confirm:1', '供应商记录保留期已核对，查询为 not_found', True, True)
    # Replay answers; a second determination is refused; the precheck shows it, never its reason.
    assert owner.confirm(intent, key='owner-confirm-key-0001').json()['replayed'] is True
    assert refused(owner.confirm(intent)) == 'already_determined'
    shown = owner.precheck(intent).json()
    assert shown['code'] == 'already_determined' and shown['determination']['confirmation_ref'] == 'owner-confirm:1' and '供应商' not in str(shown)
    with governed_effect_executor.current_executor() as executor:
        assert executor.requery_stale_unknown(intent_id=intent, confirmation_ref='owner-requery:9')['reason'] == 'not_unknown'
    # Append-only and no application role writes it or runs the handler.
    with pytest.raises(psycopg.Error), admin.transaction():
        admin.execute("update runtime.nexloop_effect_determinations set reason='x' where intent_id=%s", (intent,))
    for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_action_worker', 'nexloop_scheduler', 'nexloop_configurator'):
        assert admin.execute("select has_table_privilege(%s,'runtime.nexloop_effect_determinations','select')", (role,)).fetchone() == (False,)
        assert admin.execute("select has_function_privilege(%s,'runtime.nexloop_governed_confirm_not_delivered(text,text,text,text,text,jsonb)','execute')",
                             (role,)).fetchone() == (False,)


@PLAN
def test_any_provider_acceptance_or_a_short_unknown_blocks_the_determination(owner_desk, admin, governed_effect_executor):
    c = owner_desk;c['executor_fixture'] = governed_effect_executor
    owner = c['owner']()
    accepted, tenant = unknown_intent(c, admin)
    observe(admin, accepted, tenant, 'accepted', 60)
    observe(admin, accepted, tenant, 'not_found', 30)
    assert owner.precheck(accepted).json()['code'] == 'no_not_found_evidence'  # the provider once had it: never "not delivered"
    recent, tenant = unknown_intent(c, admin, unknown_hours=2)
    observe(admin, recent, tenant, 'not_found', 30)
    check = owner.precheck(recent).json()
    assert check['code'] == 'wait_period' and check['wait_until'] > check['latest_observation']['observed_at']  # 24 h after the first unknown
    assert states(admin, accepted) == ('unknown', 'unknown') and states(admin, recent) == ('unknown', 'unknown')


def reply_of(admin, intent, tenant, run_id):
    """The intent as an Agent reply to one inbound message (the outbound record 0079 writes with an accepted reply)."""
    conversation, message = secrets.token_hex(32), secrets.token_hex(32)
    consumer = admin.execute("select object_id from ontology.objects where tenant_id=%s and type_name='Consumer' limit 1", (tenant,)).fetchone()[0]
    admin.execute("insert into runtime.nexloop_conversations(tenant_id,world,conversation_id,consumer_id,principal_id,idempotency_key) values(%s,'real',%s,%s,'synthetic-customer',%s)",
                  (tenant, conversation, consumer, 'conv-' + conversation[:16]))
    admin.execute("insert into runtime.nexloop_outbound_messages(tenant_id,world,intent_id,receipt_id,conversation_id,trigger_message_id,run_id,sender_kind,sender_principal,"
                  "role_ref,body_digest,delivery_state) values(%s,'real',%s,gen_random_uuid(),%s,%s,%s,'agent','synthetic-agent','role:synthetic',%s,'unknown')",
                  (tenant, intent, conversation, message, run_id, 'a' * 64))
    return message


@PLAN
def test_the_reply_follow_up_is_the_owners_or_one_fallback(owner_desk, admin, governed_effect_executor):
    c = owner_desk;c['executor_fixture'] = governed_effect_executor
    owner = c['owner']();run_id = governed_effect_executor.runs[0].run_id
    escalation = "select reason,detail->>'intent_id' from control.nexloop_reply_escalations where tenant_id=%s and message_id=%s"
    delivery = 'select delivery_state from runtime.nexloop_outbound_messages where intent_id=%s'
    # Default: the owner follows up; the message is escalated, so the reply guarantee settles it without a fallback.
    first, tenant = unknown_intent(c, admin)
    message = reply_of(admin, first, tenant, run_id)
    observe(admin, first, tenant, 'not_found', 30)
    assert owner.precheck(first).json()['reply_message_escalated'] is False
    assert owner.confirm(first).json()['reply_followup'] == 'owner_follow_up'
    assert admin.execute(escalation, (tenant, message)).fetchone() == ('owner_follow_up', first)
    assert admin.execute(delivery, (first,)).fetchone() == ('failed',)
    assert admin.execute("select to_state,source from runtime.nexloop_outbound_delivery_events where intent_id=%s order by event_number desc limit 1",
                         (first,)).fetchone() == ('failed', 'intent')
    # The owner allows the one fallback: no escalation, the failed reply is left to the reply guarantee.
    second, tenant = unknown_intent(c, admin)
    message = reply_of(admin, second, tenant, run_id)
    observe(admin, second, tenant, 'not_found', 30)
    assert owner.confirm(second, confirmation_ref='owner-confirm:2', reply_followup='fallback_reply').json()['reply_followup'] == 'fallback_reply'
    assert admin.execute(escalation, (tenant, message)).fetchone() is None and admin.execute(delivery, (second,)).fetchone() == ('failed',)
    # A message already escalated (e.g. reply_unknown after reconciliation) has no fallback left: refused, nothing changed.
    third, tenant = unknown_intent(c, admin)
    message = reply_of(admin, third, tenant, run_id)
    observe(admin, third, tenant, 'not_found', 30)
    conversation, consumer = admin.execute('select o.conversation_id,v.consumer_id from runtime.nexloop_outbound_messages o join runtime.nexloop_conversations v using(tenant_id,world,conversation_id) where o.intent_id=%s',
                                           (third,)).fetchone()
    admin.execute("insert into control.nexloop_reply_escalations(tenant_id,world,message_id,conversation_id,consumer_id,reason,detail) values(%s,'real',%s,%s,%s,'reply_unknown','{}')",
                  (tenant, message, conversation, consumer))
    assert owner.precheck(third).json()['reply_message_escalated'] is True
    assert refused(owner.confirm(third, confirmation_ref='owner-confirm:3', reply_followup='fallback_reply')) == 'fallback_unavailable'
    assert states(admin, third) == ('unknown', 'unknown')
    assert owner.confirm(third, confirmation_ref='owner-confirm:3').status_code == 200  # the owner follows up instead
    assert admin.execute(escalation, (tenant, message)).fetchone()[0] == 'reply_unknown'  # the earlier escalation stays


# The owner's tool (scripts/deploy/confirm_not_delivered.py): configuration, refusals before any request, and the run against the
# production app in process (same routes, realm, cookie and governed entry as over HTTPS).
import io  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/deploy'))
import confirm_not_delivered as T  # noqa: E402
from test_real_dispatch_switch_tool import AppTransport  # noqa: E402


def tool_config(intents, **override):
    return json.dumps({'schema': T.SCHEMA, 'confirmation_ref': 'owner-confirm:tool-1', 'reason': '供应商保留期已核对', 'provider_retention_checked': True,
                       'intents': intents} | override)


def test_the_example_is_valid_and_bad_configurations_are_refused():
    example = (Path(__file__).resolve().parents[1] / 'deploy/examples/confirm-not-delivered.example.json').read_text()
    assert T.load_config(example)['intents'][0]['reply_followup'] == 'owner_follow_up'
    one = [{'intent_id': str(uuid.uuid4())}]
    assert T.load_config(tool_config(one))['intents'][0]['reply_followup'] == 'owner_follow_up'
    for bad in ({'provider_retention_checked': False}, {'confirmation_ref': 'has space'}, {'reason': ''}, {'intents': []},
                {'intents': [{'intent_id': 'nope'}]}, {'intents': one * 2}, {'intents': [{**one[0], 'reply_followup': 'none'}]}, {'extra': 1}):
        with pytest.raises(T.Refused):
            T.load_config(tool_config(**{'intents': one, **bad}))
    assert T.idempotency_key('a', 'r', 'owner_follow_up') == T.idempotency_key('a', 'r', 'owner_follow_up') != T.idempotency_key('a', 'r', 'fallback_reply')


@PLAN
def test_the_owner_tool_prechecks_confirms_and_replays(owner_desk, admin, governed_effect_executor, tmp_path, capsys):
    from test_real_dispatch_switch_tool import private as private_text
    c = owner_desk;c['executor_fixture'] = governed_effect_executor
    ready, tenant = unknown_intent(c, admin)
    observe(admin, ready, tenant, 'not_found', 30)
    early, _ = unknown_intent(c, admin)
    observe(admin, early, tenant, 'not_found', 1)
    owner = c['owner']()  # its client is the in-process app; the tool logs in on its own

    def tool(member, intents, *extra):
        cfg = private_text(tmp_path, 'confirm-' + secrets.token_hex(4) + '.json', tool_config(intents))
        user = private_text(tmp_path, 'user-' + secrets.token_hex(4), member['username'] + '\n')
        transport = AppTransport(owner.client)
        code = T.main(['--origin', ORIGIN, '--password-stdin', '--username-file', str(user), '--config', str(cfg), *extra],
                      stdin=io.StringIO(member['password'] + '\n'), transport=transport)
        out = json.loads(capsys.readouterr().out)
        assert member['password'] not in json.dumps(out) and '供应商' not in json.dumps(out, ensure_ascii=False)
        return code, out, transport.requests
    member = c['owner_member']
    code, out, requests = tool(member, [{'intent_id': ready}, {'intent_id': early}], '--dry-run')
    assert code == 1 and [i['result'] for i in out['intents']] == ['eligible', 'not_eligible'] and out['intents'][1]['before']['code'] == 'wait_period'
    assert not any(m == 'POST' and 'actions' in p for m, p in requests) and states(admin, ready) == ('unknown', 'unknown')
    code, out, _ = tool(member, [{'intent_id': ready}])
    assert code == 0 and out['intents'][0]['result'] == 'determined' and out['intents'][0]['after']['code'] == 'already_determined'
    assert states(admin, ready) == ('failed', 'failed')
    code, out, _ = tool(member, [{'intent_id': ready}])
    assert code == 0 and out['intents'][0]['result'] == 'already_determined'  # a rerun changes nothing
