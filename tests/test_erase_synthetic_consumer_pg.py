"""0220 (ADR-034 first step; AT-050 second restore drill): the owner's persona marking and the erasure of a synthetic Consumer
over the workbench HTTP entry, and the owner's tool (scripts/deploy/erase_synthetic_consumer.py) against the production app in
process. Clean catalog PostgreSQL, synthetic data; owner and operator are workbench members by trusted configuration."""
import io
import json
import secrets
import sys
from pathlib import Path

import psycopg
import pytest

from commercial_fixture import publish_registry_actions
from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_http import ORIGIN
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_restore_fence_pg import restoring  # noqa: F401
from test_erasure_pg import KEEPER
from test_workbench_actions_http import act
from test_workbench_read_pg import workbench_login

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/deploy'))
import erase_synthetic_consumer as T  # noqa: E402
from test_real_dispatch_switch_tool import AppTransport, private as private_text  # noqa: E402

PLAN = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)
ACTIONS = ('nexloop.consumer.erase', 'nexloop.consumer.set_persona', 'nexloop.consumer.erase_synthetic')


def precheck(client, consumer):
    return client.get('/api/v1/workbench/erasure-precheck/' + consumer, headers={'Origin': ORIGIN})


def persona_rows(admin, consumer):
    return (admin.execute('select persona,revision from control.nexloop_consumer_personas where consumer_id=%s', (consumer,)).fetchone(),
            admin.execute('select previous,persona,revision from control.nexloop_consumer_persona_events where consumer_id=%s order by revision', (consumer,)).fetchall())


@PLAN
def test_persona_is_the_owners_and_only_a_non_real_persona_is_erased(restoring, admin):
    c = restoring
    publish_registry_actions(admin, ACTIONS)
    consumer = c['consumer']
    with c['app']() as client:
        csrf = workbench_login(client, c['operator_member']).json()['csrf_token']
        assert act(client, csrf, 'set_consumer_persona', {'consumer_id': consumer, 'persona': 'synthetic', 'reason': 'x'}).status_code == 403
        assert act(client, csrf, 'erase_synthetic_consumer', {'consumer_id': consumer, 'reason': 'x'}).status_code == 403
        assert precheck(client, consumer).status_code == 403  # owner only
    with c['app']() as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        before = precheck(client, consumer).json()
        assert before['persona'] == 'real' and before['persona_revision'] == 0 and before['erasure'] is None and before['consumer_object'] is True
        assert set(before['held']) == set(T.HELD)
        # Unmarked means real: the synthetic erasure is refused with its fixed code; nothing is registered.
        refused = act(client, csrf, 'erase_synthetic_consumer', {'consumer_id': consumer, 'reason': '合成删除'})
        assert refused.status_code == 409 and refused.json()['details'] == {'reason': 'persona_real'}
        assert admin.execute('select count(*) from control.nexloop_consumer_erasures').fetchone() == (0,)
        assert act(client, csrf, 'set_consumer_persona', {'consumer_id': consumer, 'persona': 'tester', 'reason': 'x'}).status_code == 422
        assert act(client, csrf, 'set_consumer_persona', {'consumer_id': '0' * 64, 'persona': 'synthetic', 'reason': 'x'}).status_code == 403
        done = act(client, csrf, 'set_consumer_persona', {'consumer_id': consumer, 'persona': 'simulated_agent', 'reason': '测试账号'}, key='wb-persona-mark-0001')
        assert done.status_code == 200 and done.json()['changed'] is True and done.json()['revision'] == 1, done.text
        assert act(client, csrf, 'set_consumer_persona', {'consumer_id': consumer, 'persona': 'simulated_agent', 'reason': '测试账号'}, key='wb-persona-mark-0001').json()['replayed'] is True
        assert act(client, csrf, 'set_consumer_persona', {'consumer_id': consumer, 'persona': 'simulated_agent', 'reason': '再次'}).json()['changed'] is False
        # The undo is real again (revision 2), then synthetic (revision 3); every change is an event.
        assert act(client, csrf, 'set_consumer_persona', {'consumer_id': consumer, 'persona': 'real', 'reason': '撤销'}).json()['revision'] == 2
        assert act(client, csrf, 'set_consumer_persona', {'consumer_id': consumer, 'persona': 'synthetic', 'reason': '合成'}).json()['revision'] == 3
        assert persona_rows(admin, consumer) == (('synthetic', 3), [('real', 'simulated_agent', 1), ('simulated_agent', 'real', 2), ('real', 'synthetic', 3)])
        erased = act(client, csrf, 'erase_synthetic_consumer', {'consumer_id': consumer, 'reason': '合成删除'}, key='wb-erase-synthetic-0001')
        assert erased.status_code == 200 and erased.json()['persona'] == 'synthetic' and erased.json()['replayed'] is False, erased.text
        again = act(client, csrf, 'erase_synthetic_consumer', {'consumer_id': consumer, 'reason': '合成删除'}, key='wb-erase-synthetic-0001')
        assert again.status_code == 200 and again.json()['replayed'] is True
        after = precheck(client, consumer).json()
        assert after['erasure']['source'] == 'owner' and after['erasure']['state'] not in ('pending_confirmation',) and 'reason' not in after['erasure']
    assert admin.execute('select runtime.nexloop_consumer_erasing(%s,%s,%s)', (TENANT, 'real', consumer)).fetchone() == (True,)
    with pytest.raises(psycopg.Error, match='append-only'), admin.transaction():
        admin.execute('delete from control.nexloop_consumer_persona_events')


@PLAN
def test_the_owner_tool_marks_from_the_recipe_dry_runs_erases_and_replays(restoring, admin, tmp_path, capsys):
    c = restoring
    publish_registry_actions(admin, ACTIONS)
    consumer = c['consumer']
    recipe = private_text(tmp_path, 'webchat-consumers.json', json.dumps({'schema_version': T.RECIPE_SCHEMA, 'consumers': [
        {'principal_id': 'synthetic-webchat-customer-1', 'subject_id': 'synthetic-subject-1', 'persona': 'simulated_agent'}]}))

    def tool(member, command, config, *extra):
        cfg = private_text(tmp_path, command + '-' + secrets.token_hex(4) + '.json', json.dumps(config))
        user = private_text(tmp_path, 'user-' + secrets.token_hex(4), member['username'] + '\n')
        with c['app']() as client:
            transport = AppTransport(client)
            code = T.main([command, '--origin', ORIGIN, '--password-stdin', '--username-file', str(user), '--config', str(cfg), *extra],
                          stdin=io.StringIO(member['password'] + '\n'), transport=transport)
        out = json.loads(capsys.readouterr().out)
        text = json.dumps(out, ensure_ascii=False)
        assert member['password'] not in text and member['username'] not in text and '合成' not in text and 'synthetic-webchat-customer-1' not in text
        return code, out, transport.requests

    owner = c['owner_member']
    erase_cfg = {'schema': T.ERASE_SCHEMA, 'confirmation_ref': 'at050-erase:tool-1', 'reason': '合成顾客删除', 'consumers': [consumer]}
    # Unmarked: the dry run shows persona real and the run sends no erasure.
    code, out, requests = tool(owner, 'erase', erase_cfg)
    assert code == 1 and out['consumers'][0]['result'] == 'persona_real' and out['consumers'][0]['before']['persona'] == 'real'
    assert not any(m == 'POST' and '/actions/' in p for m, p in requests)
    mark_cfg = {'schema': T.MARK_SCHEMA, 'confirmation_ref': 'persona-mark:tool-1', 'reason': '合成测试账号',
                'consumers': [{'consumer_id': consumer, 'principal_id': 'synthetic-webchat-customer-1'}]}
    # Not in the recipe: refused before any write; the operator cannot mark.
    code, out, requests = tool(owner, 'mark', {**mark_cfg, 'consumers': [{'consumer_id': consumer, 'principal_id': 'someone-else'}]}, '--recipe', str(recipe))
    assert code == 1 and out['consumers'][0]['result'] == 'not_in_recipe' and not any(m == 'POST' and '/actions/' in p for m, p in requests)
    code, out, _ = tool(c['operator_member'], 'mark', mark_cfg, '--recipe', str(recipe))
    assert code == 1 and out['ok'] is False
    code, out, requests = tool(owner, 'mark', mark_cfg, '--recipe', str(recipe), '--dry-run')
    assert code == 0 and out['consumers'][0]['result'] == 'would_mark' and not any(m == 'POST' and '/actions/' in p for m, p in requests)
    code, out, _ = tool(owner, 'mark', mark_cfg, '--recipe', str(recipe))
    assert code == 0 and out['consumers'][0] | {'consumer_id': None} == {'consumer_id': None, 'persona_before': 'real', 'persona_revision': 0,
                                                                         'target': 'simulated_agent', 'result': 'marked', 'persona': 'simulated_agent', 'revision': 1}
    code, out, _ = tool(owner, 'mark', mark_cfg, '--recipe', str(recipe))
    assert code == 0 and out['consumers'][0]['result'] == 'already_marked'
    # Erase: dry run (no write), the erasure, a rerun (already erasing), the operator refused.
    code, out, requests = tool(owner, 'erase', erase_cfg, '--dry-run')
    assert code == 0 and out['consumers'][0]['result'] == 'would_erase' and out['consumers'][0]['before']['persona'] == 'simulated_agent'
    assert not any(m == 'POST' and '/actions/' in p for m, p in requests)
    assert admin.execute('select count(*) from control.nexloop_consumer_erasures').fetchone() == (0,)
    code, out, _ = tool(c['operator_member'], 'erase', erase_cfg)
    assert code == 1 and out['ok'] is False
    code, out, _ = tool(owner, 'erase', erase_cfg)
    assert code == 0 and out['consumers'][0]['result'] == 'erasing' and out['consumers'][0]['after']['erasure']['source'] == 'owner', out
    code, out, _ = tool(owner, 'erase', erase_cfg)
    assert code == 0 and out['consumers'][0]['result'] in ('already_erasing', 'already_erased')
    reason = admin.execute('select reason from control.nexloop_consumer_erasures where consumer_id=%s', (consumer,)).fetchone()[0]
    assert reason == 'at050-erase:tool-1: 合成顾客删除'


def test_the_examples_are_valid_and_bad_configurations_are_refused():
    root = Path(__file__).resolve().parents[1] / 'deploy/examples'
    assert T.load_mark_config((root / 'consumer-persona.example.json').read_text())['consumers'][0]['principal_id']
    assert T.load_erase_config((root / 'erase-synthetic-consumer.example.json').read_text())['consumers']
    good = {'schema': T.ERASE_SCHEMA, 'confirmation_ref': 'r-1', 'reason': 'x', 'consumers': ['a' * 64]}
    for bad in ({'consumers': []}, {'consumers': ['nope']}, {'consumers': ['a' * 64] * 2}, {'confirmation_ref': 'has space'}, {'reason': ''},
                {'reason': 'x' * 499}, {'extra': 1}, {'schema': T.MARK_SCHEMA}):
        with pytest.raises(T.Refused):
            T.load_erase_config(json.dumps(good | bad))
    with pytest.raises(T.Refused):
        T.load_recipe(json.dumps({'schema_version': 'other', 'consumers': []}))
    assert T.load_recipe(json.dumps({'schema_version': T.RECIPE_SCHEMA, 'consumers': [{'principal_id': 'p', 'persona': 'simulated_agent'},
                                                                                       {'principal_id': 'q', 'persona': 'simulated_agent', 'revoke': True}]})) == {'p': 'simulated_agent'}
    assert T.idempotency_key('erase-synthetic', 'a', 'r') == T.idempotency_key('erase-synthetic', 'a', 'r') != T.idempotency_key('erase-synthetic', 'a', 'r2')


@PLAN
def test_the_post_check_shows_messages_claims_and_exports_cleared(restoring, admin):  # noqa: F811
    """After the keeper finishes, the owner's precheck shows the erasure completed and nothing left: no message body, no Claim
    value, no data export (the run sheet's post-check)."""
    c = restoring
    publish_registry_actions(admin, ACTIONS)
    consumer = c['consumer']
    c.inbound('我是合成顾客，请删除我的资料')
    reply = c.outbound('好的，已为您登记')
    c.claim(reply, '已为您登记')
    with c['app']() as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        held = precheck(client, consumer).json()['held']
        assert held['messages_with_body'] >= 2 and held['claims_not_erased'] >= 1, held
        assert act(client, csrf, 'set_consumer_persona', {'consumer_id': consumer, 'persona': 'synthetic', 'reason': '合成'}).status_code == 200
        assert act(client, csrf, 'erase_synthetic_consumer', {'consumer_id': consumer, 'reason': '合成删除'}).status_code == 200
    from multi_authority_fixture import seed_multi_authority
    from nexloop_eios import retention
    from nexloop_eios.authorization import authenticate_service
    _, token = seed_multi_authority(admin, c['worker'], KEEPER, identity_suffix='-retention-keeper', tenant=TENANT)
    for _ in range(10):
        retention.RetentionKeeper(c['worker'], authenticate_service(c['worker'], token, world='real'), c['signer'], host=lambda run_id: 'purged').run_once()
        if admin.execute('select state from control.nexloop_consumer_erasures where consumer_id=%s', (consumer,)).fetchone()[0] == 'completed':
            break
    with c['app']() as client:
        workbench_login(client, c['owner_member'])
        after = precheck(client, consumer).json()
    assert after['erasure']['state'] == 'completed' and after['erasure']['counts']['messages'] >= 2 and after['consumer_object'] is False
    assert after['held']['messages_with_body'] == 0 and after['held']['claims_not_erased'] == 0 and after['held']['data_exports'] == 0, after['held']
    # 0221 (Codex review, PR 13): the marker row leaves with the Consumer (its events keep a pseudonym); a replayed erasure is not
    # checked again, so nothing depends on it after the erasure.
    assert after['persona'] == 'real' and after['persona_revision'] == 0
