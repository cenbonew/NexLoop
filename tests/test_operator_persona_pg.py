"""Operator persona slice 1 (0221 temporary; docs/design/operator-persona-design.md, owner decisions 2026-10-19): immutable persona
versions the owner publishes or retires, assignment (owner > segment rules > default) kept stable with an event on every change,
the tenant's AI disclosure (default none / deflect / block_human_claims false), and the Context section persona@version. Over
the workbench HTTP entry with real owner and operator logins; clean catalog PostgreSQL; synthetic data."""
import threading
import time

import psycopg
import pytest
from psycopg.types.json import Jsonb

from commercial_fixture import publish_registry_actions
from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_http import ORIGIN
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_restore_fence_pg import restoring  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import workbench_login

pytestmark = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)
ACTIONS = ('nexloop.operator_persona.publish', 'nexloop.operator_persona.assign', 'nexloop.operator_persona.rules_publish', 'nexloop.ai_disclosure.set')


def document(name='小禾', **extra):
    return {'display_name': name, 'signature': name + '｜服务助理', 'self_intro': '您好，我是' + name + '。', 'tone': '亲切、简洁',
            'values': ['先解决问题再解释', '不确定就说不确定'], 'expression': {'max_chars': 200, 'emoji': 'few', 'forbidden_phrases': ['亲爱的']},
            'scenarios': ['售后'], 'ai_disclosure': {'deflect_statement': '我是本店的服务助手，有什么可以帮您？'}, **extra}


def publish(client, csrf, persona, version, status='published', doc=None, **kw):
    return act(client, csrf, 'publish_operator_persona', {'persona_id': persona, 'version': version, 'status': status,
               'document': (doc or document()) if status == 'published' else None, 'reason': '发布'}, **kw)


def section(admin, consumer):
    return admin.execute('select runtime.nexloop_operator_persona_section(%s,%s,%s)', (TENANT, 'real', consumer)).fetchone()[0]


def overview(client, consumer=None):
    return client.get('/api/v1/workbench/operator-personas' + (f'?consumer_id={consumer}' if consumer else ''), headers={'Origin': ORIGIN})


def settle(admin, consumer):
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT,))
        return admin.execute("select runtime.nexloop_operator_persona_settle(%s,'real',%s,'synthetic-context','context:test',null)", (TENANT, consumer)).fetchone()[0]


def events(admin, consumer):
    return admin.execute('select previous,persona_id,source from control.nexloop_operator_persona_assignment_events where consumer_id=%s order by event_id', (consumer,)).fetchall()


def test_versions_rules_assignment_and_ai_disclosure_are_the_owners(restoring, admin):
    c = restoring
    publish_registry_actions(admin, ACTIONS)
    consumer = c['consumer']
    with c['app']() as client:
        csrf = workbench_login(client, c['operator_member']).json()['csrf_token']
        assert publish(client, csrf, 'xiaohe', 1).status_code == 403
        assert act(client, csrf, 'set_ai_disclosure', {'mode': 'none', 'when_asked': 'handoff', 'block_human_claims': False, 'content_label': False, 'reason': 'x'}).status_code == 403
        assert overview(client).status_code == 403
    assert section(admin, consumer) is None  # no persona: no section, packs unchanged
    with c['app']() as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        first = overview(client).json()
        assert first['personas'] == [] and first['rules'] is None
        assert first['ai_disclosure'] == {'mode': 'none', 'when_asked': 'deflect', 'block_human_claims': False, 'content_label': False, 'revision': 0}
        # Versions: next number only, immutable, a bad document refused with its code.
        bad = publish(client, csrf, 'xiaohe', 1, doc={**document(), 'price_rule': '八折'})
        assert bad.status_code == 409 and bad.json()['details'] == {'reason': 'document_invalid'}
        assert publish(client, csrf, 'xiaohe', 2).json()['details'] == {'reason': 'version_not_next'}
        # Codex review (PR 13): a missing required field makes the SQL check NULL; NULL is refused, never stored.
        expression = document()['expression']
        for broken in ({}, {k: v for k, v in document().items() if k != 'display_name'}, {k: v for k, v in document().items() if k != 'tone'},
                       {k: v for k, v in document().items() if k != 'expression'}, {**document(), 'expression': {k: v for k, v in expression.items() if k != 'max_chars'}},
                       {**document(), 'expression': {k: v for k, v in expression.items() if k != 'emoji'}}, {**document(), 'display_name': ''},
                       {**document(), 'values': [1]}, {**document(), 'ai_disclosure': {'statement': 5}}):
            refused = act(client, csrf, 'publish_operator_persona', {'persona_id': 'xiaohe', 'version': 1, 'status': 'published', 'document': broken, 'reason': '发布'})
            assert refused.status_code == 409 and refused.json()['details'] == {'reason': 'document_invalid'}, broken
        assert admin.execute('select count(*) from control.nexloop_operator_personas').fetchone() == (0,)
        assert act(client, csrf, 'publish_persona_rules', {'version': 1, 'default_persona_id': None, 'rules': [{}], 'reason': 'x'}).json()['details'] == {'reason': 'invalid'}
        done = publish(client, csrf, 'xiaohe', 1, key='wb-persona-publish-0001')
        assert done.status_code == 200 and done.json()['persona_ref'] == 'operator-persona:xiaohe@1' and done.json()['changed'] is True, done.text
        assert publish(client, csrf, 'xiaohe', 1, key='wb-persona-publish-0001').json()['replayed'] is True
        assert publish(client, csrf, 'xiaohe', 1).json()['changed'] is False  # same document again: nothing changes
        assert publish(client, csrf, 'xiaohe', 1, doc=document('阿禾')).json()['details'] == {'reason': 'version_exists'}
        assert publish(client, csrf, 'xiaohe', 2, doc=document(tone='专业')).status_code == 200
        assert publish(client, csrf, 'lan', 1, doc=document('小蓝')).status_code == 200
        # Assignment: rules (default), then the owner's per-Consumer assignment, then cleared back to the rules.
        assert section(admin, consumer) is None  # published but not assigned and no rules yet
        missing = act(client, csrf, 'publish_persona_rules', {'version': 1, 'default_persona_id': 'nobody', 'rules': [], 'reason': '默认'})
        assert missing.json()['details'] == {'reason': 'not_published'}
        assert act(client, csrf, 'publish_persona_rules', {'version': 1, 'default_persona_id': 'xiaohe', 'rules': [{'when': {'property': 'tier'}, 'persona_id': 'lan'}],
                                                           'reason': '默认'}).status_code == 409  # a property rule needs equals
        assert act(client, csrf, 'publish_persona_rules', {'version': 1, 'default_persona_id': 'xiaohe', 'rules': [], 'reason': '默认人设'}).json()['changed'] is True
        got = section(admin, consumer)
        assert got['persona_ref'] == 'operator-persona:xiaohe@2' and got['source'] == 'default' and got['document']['tone'] == '专业'
        assert got['ai_disclosure'] == {'mode': 'none', 'when_asked': 'deflect', 'block_human_claims': False, 'content_label': False}
        first_event = settle(admin, consumer)
        assert settle(admin, consumer) == first_event and events(admin, consumer) == [(None, 'xiaohe', 'initial')]  # stable: no new event
        assigned = act(client, csrf, 'assign_operator_persona', {'consumer_id': consumer, 'persona_id': 'lan', 'reason': '老客户偏好'})
        assert assigned.status_code == 200 and assigned.json()['changed'] is True, assigned.text
        assert section(admin, consumer)['persona_ref'] == 'operator-persona:lan@1' and section(admin, consumer)['source'] == 'owner_assign'
        assert act(client, csrf, 'assign_operator_persona', {'consumer_id': consumer, 'persona_id': 'ghost', 'reason': 'x'}).json()['details'] == {'reason': 'not_published'}
        assert act(client, csrf, 'assign_operator_persona', {'consumer_id': consumer, 'persona_id': None, 'reason': '回到规则'}).json()['changed'] is True
        assert events(admin, consumer) == [(None, 'xiaohe', 'initial'), ('xiaohe', 'lan', 'owner_assign'), ('lan', 'xiaohe', 'owner_clear')]
        # A rule version matching a property of this Consumer object: the change settles at the next bind, as a rule change.
        # Fixture seeding (the fixture's Consumer has no properties): one synthetic segment property, injected by admin.
        with admin.transaction():
            admin.execute('set local session_replication_role=replica')
            admin.execute("update ontology.objects set properties=properties||'{\"tier\":\"gold\"}'::jsonb where type_name='Consumer' and object_id=%s", (consumer,))
        name, value = 'tier', 'gold'
        assert act(client, csrf, 'publish_persona_rules', {'version': 2, 'default_persona_id': 'xiaohe',
            'rules': [{'when': {'property': name, 'equals': value + '-other'}, 'persona_id': 'xiaohe'}, {'when': {'property': name, 'equals': value}, 'persona_id': 'lan'}],
            'reason': '按属性'}).status_code == 200
        assert section(admin, consumer)['persona_ref'] == 'operator-persona:lan@1' and section(admin, consumer)['source'] == 'rule'
        settle(admin, consumer)
        assert events(admin, consumer)[-1] == ('xiaohe', 'lan', 'rule_change')
        # Retiring the persona in use: the Consumer falls back to the default with persona_retired at the next settle.
        assert act(client, csrf, 'publish_persona_rules', {'version': 3, 'default_persona_id': 'xiaohe', 'rules': [], 'reason': '只用默认'}).status_code == 200
        assert act(client, csrf, 'assign_operator_persona', {'consumer_id': consumer, 'persona_id': 'lan', 'reason': '指定'}).status_code == 200
        assert publish(client, csrf, 'lan', 1, status='retired').status_code == 200
        assert section(admin, consumer)['persona_ref'] == 'operator-persona:xiaohe@2'  # the assignment names a retired persona: rules apply
        settle(admin, consumer)
        assert events(admin, consumer)[-1] == ('lan', 'xiaohe', 'persona_retired')
        assert publish(client, csrf, 'lan', 1, doc=document('小蓝')).status_code == 200  # republishing the same document makes it usable again
        # AI disclosure: owner D7/D8.
        ai = act(client, csrf, 'set_ai_disclosure', {'mode': 'none', 'when_asked': 'handoff', 'block_human_claims': True, 'content_label': False, 'reason': '转人工'})
        assert ai.status_code == 200 and ai.json()['revision'] == 1 and ai.json()['changed'] is True
        assert act(client, csrf, 'set_ai_disclosure', {'mode': 'sometimes', 'when_asked': 'handoff', 'block_human_claims': True, 'content_label': False, 'reason': 'x'}).status_code == 422
        view = overview(client, consumer).json()
        assert view['ai_disclosure']['when_asked'] == 'handoff' and {p['persona_id'] for p in view['personas']} == {'xiaohe', 'lan'}
        # Settled stays until the next bind; desired already names the republished assignment.
        assert view['consumer']['settled']['persona_id'] == 'xiaohe' and view['consumer']['desired'] == {'persona_id': 'lan', 'source': 'owner_assign', 'rules_version': None}
        assert len(view['consumer']['events']) >= 4
    with pytest.raises(psycopg.Error, match='append-only'), admin.transaction():
        admin.execute("update control.nexloop_operator_personas set document=%s", (Jsonb({}),))
    with pytest.raises(psycopg.Error, match='append-only'), admin.transaction():
        admin.execute('delete from control.nexloop_operator_persona_assignment_events')


def seed_two_personas(client, csrf, consumer):
    assert publish(client, csrf, 'xiaohe', 1).status_code == 200
    assert publish(client, csrf, 'lan', 1, doc=document('小蓝')).status_code == 200
    assert act(client, csrf, 'publish_persona_rules', {'version': 1, 'default_persona_id': 'xiaohe', 'rules': [], 'reason': '默认人设'}).status_code == 200


def test_a_bind_settling_beside_the_owners_assignment_never_reverts_it(restoring, admin, pg):
    """Codex review (PR 13): the owner's assignment and a Context bind's settle are serialized per Consumer; the settle computes
    the desired persona only after the lock, so it never overwrites the new assignment or records a false rule_change."""
    c = restoring
    publish_registry_actions(admin, ACTIONS)
    consumer = c['consumer']
    with c['app']() as client:
        seed_two_personas(client, workbench_login(client, c['owner_member']).json()['csrf_token'], consumer)
    settle(admin, consumer)  # settled on the default (initial)
    with psycopg.connect(pg) as a, psycopg.connect(pg) as b:
        a.execute("set role nexloop_owner");a.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
        a.execute("select control.nexloop_governed_assign_operator_persona(%s,'real','synthetic-owner','human','race-assign',%s)",
                  (TENANT, Jsonb({'operation': 'assign_operator_persona', 'request_id': 'race-assign', 'consumer_id': consumer, 'persona_id': 'lan', 'reason': '指定'})))
        result = {}

        def bind_settle():
            with b.transaction():
                b.execute("set local role nexloop_owner");b.execute("select set_config('eios.tenant_id',%s,true)", (TENANT,))
                result['event'] = b.execute("select runtime.nexloop_operator_persona_settle(%s,'real',%s,'synthetic-context','context:race',null)", (TENANT, consumer)).fetchone()[0]
        worker = threading.Thread(target=bind_settle)
        worker.start()
        deadline = time.monotonic() + 10
        while not admin.execute("select count(*) from pg_stat_activity where wait_event_type='Lock' and query like '%%operator_persona_settle%%'").fetchone()[0]:
            assert time.monotonic() < deadline, 'the settle did not wait for the assignment'
            time.sleep(0.05)
        a.commit()
        worker.join(10)
    assert events(admin, consumer) == [(None, 'xiaohe', 'initial'), ('xiaohe', 'lan', 'owner_assign')]  # no rule_change back to xiaohe
    assert admin.execute('select persona_id,source from control.nexloop_operator_persona_current where consumer_id=%s', (consumer,)).fetchone() == ('lan', 'owner_assign')


def test_consumer_erasure_removes_the_assignment_and_pseudonymizes_its_events(restoring, admin):
    """Codex review (PR 13): after the Consumer's erasure no persona table names the Consumer: assignment and settled rows are
    gone, assignment events (and ADR-034 persona events) keep persona ids and times under a pseudonym without request or reason."""
    from multi_authority_fixture import seed_multi_authority
    from nexloop_eios import retention
    from nexloop_eios.authorization import authenticate_service
    from test_erasure_pg import KEEPER
    c = restoring
    publish_registry_actions(admin, ACTIONS + ('nexloop.consumer.erase', 'nexloop.consumer.set_persona'))
    consumer = c['consumer']
    with c['app']() as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        seed_two_personas(client, csrf, consumer)
        assert act(client, csrf, 'assign_operator_persona', {'consumer_id': consumer, 'persona_id': 'lan', 'reason': '顾客王先生要求换人'}).status_code == 200
        assert act(client, csrf, 'set_consumer_persona', {'consumer_id': consumer, 'persona': 'synthetic', 'reason': '王先生是测试账号'}).status_code == 200
        assert act(client, csrf, 'erase_consumer', {'consumer_id': consumer, 'reason': '删除'}).status_code == 200
    _, token = seed_multi_authority(admin, c['worker'], KEEPER, identity_suffix='-retention-keeper', tenant=TENANT)
    for _ in range(12):
        retention.RetentionKeeper(c['worker'], authenticate_service(c['worker'], token, world='real'), c['signer'], host=lambda run_id: 'purged').run_once()
        if admin.execute('select state from control.nexloop_consumer_erasures where consumer_id=%s', (consumer,)).fetchone()[0] == 'completed':
            break
    state, counts = admin.execute('select state,counts from control.nexloop_consumer_erasures where consumer_id=%s', (consumer,)).fetchone()
    assert state == 'completed' and counts['operator_persona_events'] == 1 and counts['persona_events'] == 1, counts
    for table in ('nexloop_operator_persona_assignments', 'nexloop_operator_persona_current', 'nexloop_operator_persona_assignment_events',
                  'nexloop_consumer_personas', 'nexloop_consumer_persona_events'):
        assert admin.execute(f'select count(*) from control.{table} where consumer_id=%s', (consumer,)).fetchone() == (0,), table
    rows = admin.execute('select consumer_id,persona_id,source,request_intent,reason from control.nexloop_operator_persona_assignment_events').fetchall()
    assert len(rows) == 1 and rows[0][0].startswith('pseudonym:') and rows[0][1:] == ('lan', 'owner_assign', None, None)
    marker = admin.execute('select consumer_id,persona,request_intent,reason from control.nexloop_consumer_persona_events').fetchall()
    assert len(marker) == 1 and marker[0][0] == rows[0][0] and marker[0][1:] == ('synthetic', '', '')
    text = str(admin.execute('select jsonb_agg(t) from control.nexloop_operator_persona_assignment_events t').fetchone()[0])
    assert '王先生' not in text

