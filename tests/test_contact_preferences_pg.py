"""Consumer communication preferences (0231 temporary; docs/design/operator-persona-design.md §4, D6, D9): only the consumer's own
statements count; quiet hours, frequency, channel and topic refuse a contact at dispatch (NXC06) after the contact restriction
(ADR-023, NXC05) and never hold back a reply bound to an inbound message; the owner may decline a preference, never add one.
Clean catalog PostgreSQL; actual governed Run submissions and the independent effect Worker with a loopback provider (zero
requests when refused); admin seeds the consumer's messages and their Claims (NX-019 output) as in the commitment tests."""
import hashlib
import secrets
from datetime import UTC, datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.types.json import Jsonb
from support.effect_provider import effect_provider

from commercial_fixture import publish_registry_actions
from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_http import ORIGIN
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_contact_effect_categories_pg import REFUSAL, contact_check, refused
from test_restore_fence_pg import restoring  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import workbench_login

pytestmark = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)
SHANGHAI = timezone(timedelta(hours=8))


def statement(c, predicate, value, *, polarity='affirmed', kind='preference', speaker='consumer', resolution='unresolved', modality='requested'):
    """One consumer message and the Claim NX-019 would extract from it (admin seed)."""
    a, consumer = c['admin'], c['consumer']
    body = f'{predicate}:{value}'
    message = c.inbound(body)
    conversation = c.conversation(consumer)
    claim_id = secrets.token_hex(32)
    typed = {'type': 'number', 'value': value} if isinstance(value, int) else {'type': 'string', 'value': value}
    valid_time = {'expression': '', 'kind': 'none', 'status': 'absent', 'start': None, 'end': None, 'timezone': 'Asia/Shanghai', 'anchor': datetime.now(UTC).isoformat()}
    a.execute('''insert into ontology.nexloop_claims(tenant_id,world,claim_id,conversation_id,consumer_id,first_input_digest,topic_key,subject_kind,subject_ref,subject_text,
        predicate,value,speaker,polarity,modality,condition_text,time_expression,valid_time,source_message_id,source_sequence,span_start,span_end,source_content_hash,quote,
        extractor_version,confidence,epistemic_kind,resolution_state,derived_from,correlation_key,guard_flags)
        values(%s,'real',%s,%s,%s,%s,'contact','consumer',%s,'顾客',%s,%s,%s,%s,%s,'','',%s,%s,1,0,%s,%s,%s,'synthetic-extractor',0.9,%s,%s,'[]',%s,'[]')''',
        (TENANT, claim_id, conversation, consumer, 'e' * 64, 'consumer:' + consumer, predicate, Jsonb(typed), speaker, polarity, modality, Jsonb(valid_time),
         message, len(body), hashlib.sha256(body.encode()).hexdigest(), body, kind, resolution, secrets.token_hex(32)))
    return claim_id


def window_around_now(minutes=60):
    now = datetime.now(SHANGHAI)
    return (now - timedelta(minutes=minutes)).strftime('%H:%M') + '-' + (now + timedelta(minutes=minutes)).strftime('%H:%M')


def refusal(c, *, channel=None, topic=None):
    return c['admin'].execute("select runtime.nexloop_contact_preference_refusal(%s,'real',%s,%s,%s,gen_random_uuid(),clock_timestamp())",
                              (TENANT, c['consumer'], channel, topic)).fetchone()[0]


def test_dispatch_refuses_a_contact_the_consumer_asked_not_to_receive_and_never_a_reply(commitments, admin, tmp_path):
    c = commitments
    c.configure_categories()
    notify = c.submit({'service': '退款处理', 'message': '您的退款已处理'})['intent_id']  # a notification: contacts the customer
    service = c.submit({'service': '后台退款处理'})['intent_id']  # no notification: not a contact
    assert contact_check(c, notify) == 'pass'
    # Quiet hours stated by the consumer, around now (Asia/Shanghai): the notification is refused at dispatch, zero requests.
    statement(c, 'contact_quiet_hours', window_around_now())
    assert contact_check(c, notify) == 'NXC06 contact preference: preference_quiet_hours'
    assert contact_check(c, service) == 'pass'
    reply = c.outbound('好的，已为您处理')  # a reply bound to an inbound message: never held back by time or frequency
    assert contact_check(c, str(reply['intent_id'])) == 'pass'
    with effect_provider(tmp_path / 'provider.sqlite') as provider:
        refused(c, provider)
    # A hypothesis, an agent's statement and an unparseable window do not count; neither does a superseded statement.
    c2 = statement(c, 'contact_quiet_hours', '晚点再说')
    statement(c, 'contact_quiet_hours', window_around_now(), kind='hypothesis', resolution='hypothesis_only')
    statement(c, 'contact_quiet_hours', window_around_now(), speaker='agent')
    dims = admin.execute("select jsonb_agg(x->>'dimension') from jsonb_array_elements(runtime.nexloop_contact_preferences(%s,'real',%s)) x", (TENANT, c['consumer'])).fetchone()[0]
    assert dims == ['quiet_hours'] and c2
    # Frequency 0 in 7 days, an "only this channel" statement, a refused topic.
    admin.execute("update ontology.nexloop_claims set superseded_at=clock_timestamp(),resolution_state='superseded' where predicate='contact_quiet_hours'")
    assert contact_check(c, notify) == 'pass'
    statement(c, 'contact_frequency_max', 0)
    assert contact_check(c, notify) == 'NXC06 contact preference: preference_frequency'
    admin.execute("update ontology.nexloop_claims set superseded_at=clock_timestamp() where predicate='contact_frequency_max'")
    statement(c, '联系渠道', 'wechat')
    assert refusal(c, channel='wechat') is None and refusal(c, channel='sms') == 'preference_channel' and refusal(c) == 'preference_channel'
    assert contact_check(c, notify) == 'NXC06 contact preference: preference_channel'  # the intent names no channel: refused
    admin.execute("update ontology.nexloop_claims set superseded_at=clock_timestamp() where predicate='联系渠道'")
    statement(c, 'contact_topic', 'promotion', polarity='negated')
    assert refusal(c, topic='promotion') == 'preference_topic' and refusal(c, topic='service_followup') is None and refusal(c) is None
    statement(c, 'contact_topic', 'promotion')  # "you may promote to me" adds nothing (D9: only tightening)
    assert refusal(c, topic='promotion') == 'preference_topic'
    # The contact restriction stays first: a refusing message makes it NXC05, whatever the preferences.
    c.inbound(REFUSAL)
    assert contact_check(c, notify).startswith('NXC05')


def test_the_owner_sees_and_may_decline_a_preference_and_the_model_sees_the_rest(restoring, admin):
    c = restoring
    publish_registry_actions(admin, ('nexloop.contact_preference.dismiss', 'nexloop.operator_persona.publish', 'nexloop.operator_persona.rules_publish'))
    consumer = c['consumer']
    tone = statement(c, 'communication_tone', '说简单点')
    quiet = statement(c, '勿扰时段', '22:00-08:00')
    other = statement(c, '预算上限', '两千', kind='constraint')
    with c['app']() as client:
        csrf = workbench_login(client, c['operator_member']).json()['csrf_token']
        assert client.get(f'/api/v1/workbench/consumers/{consumer}/contact-preferences', headers={'Origin': ORIGIN}).status_code == 403
        assert act(client, csrf, 'dismiss_contact_preference', {'claim_id': quiet, 'dismissed': True, 'reason': 'x'}).status_code == 403
    with c['app']() as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        view = client.get(f'/api/v1/workbench/consumers/{consumer}/contact-preferences', headers={'Origin': ORIGIN}).json()
        assert sorted((p['claim_id'], p['dimension'], p['value'], p['dismissed']) for p in view['preferences']) == sorted([(tone, 'tone', '说简单点', False), (quiet, 'quiet_hours', '22:00-08:00', False)])
        assert '说简单点' not in str([p.get('quote') for p in view['preferences']])  # no quote field
        assert act(client, csrf, 'dismiss_contact_preference', {'claim_id': other, 'dismissed': True, 'reason': 'x'}).json()['details'] == {'reason': 'not_a_preference'}
        done = act(client, csrf, 'dismiss_contact_preference', {'claim_id': quiet, 'dismissed': True, 'reason': '顾客已改口'})
        assert done.status_code == 200 and done.json()['revision'] == 1, done.text
        assert act(client, csrf, 'dismiss_contact_preference', {'claim_id': quiet, 'dismissed': True, 'reason': '再次'}).json()['changed'] is False
        # The Context section carries the consumer's remaining preferences (once the tenant has a persona).
        doc = {'display_name': '小禾', 'tone': '亲切', 'expression': {'max_chars': 200, 'emoji': 'none'}}
        assert act(client, csrf, 'publish_operator_persona', {'persona_id': 'xiaohe', 'version': 1, 'status': 'published', 'document': doc, 'reason': '发布'}).status_code == 200
        assert act(client, csrf, 'publish_persona_rules', {'version': 1, 'default_persona_id': 'xiaohe', 'rules': [], 'reason': '默认'}).status_code == 200
    section = admin.execute("select runtime.nexloop_operator_persona_section(%s,'real',%s)", (TENANT, consumer)).fetchone()[0]
    assert section['preferences'] == [{'claim_id': tone, 'dimension': 'tone', 'polarity': 'affirmed', 'value': '说简单点'}]
    with pytest.raises(psycopg.Error), admin.transaction():
        admin.execute("select control.nexloop_governed_dismiss_contact_preference(%s,'real','p','human','i',%s)",
                      (TENANT, Jsonb({'operation': 'dismiss_contact_preference', 'claim_id': secrets.token_hex(32), 'dismissed': True, 'reason': 'x'})))
