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
from test_real_dispatch_switch_pg import owner_sql

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
    # 0227 (Codex review, PR 13): the marker row leaves with the Consumer (its events keep a pseudonym); a replayed erasure is not
    # checked again, so nothing depends on it after the erasure.
    assert after['persona'] == 'real' and after['persona_revision'] == 0



ROLE_FILE = Path(__file__).resolve().parents[1] / 'deploy/authorization/webchat-consumer-role.v1.json'


def webchat_customer(c, admin, label, grants='granted'):
    """0429: a WebChat customer as stage has them: a browser Human (identity rows), its 0050 grants compiled by webchat_consumers
    ('granted', 'revoked' or 'none' = no grant fact at all), a Consumer created and owned through the service registrar (the real
    ConsumerOwnership command), persona synthetic. No browser identity link: the WebChat role never grants it."""
    from multi_authority_fixture import seed_multi_authority
    from nexloop_eios import webchat_consumers as W
    from nexloop_eios.authorization import authenticate_service
    from nexloop_eios.conversation_messages import ConversationOwnershipRegistrar
    from nexloop_eios.object_actions import GovernedObjectCreator
    from system_catalog_fixture import publish_system_catalog
    from test_workbench_read_pg import add_human, write_facts
    from eios.authz.operations import Operation
    from eios.authz.resources import ResourceType
    human = add_human(admin, label)
    role = json.loads(ROLE_FILE.read_text())
    if grants != 'none':
        write_facts(admin, W.compile_recipe(role, {'schema_version': W.CONSUMERS_SCHEMA, 'tenant_id': TENANT, 'browser_application_id': 'synthetic-webchat',
            'caller_application_id': 'eios:application:nexloop-webchat', 'consumers': [{'principal_id': human['principal_id'], 'subject_id': human['subject_id'],
            'persona': 'simulated_agent', **({'revoke': True} if grants == 'revoked' else {})}]})['authority_facts'])
    pool, signer = c['plan']['backend']._pool, c['signer']
    if not c.get('registrar_token'):
        publish_system_catalog(admin, TENANT, ('ConsumerOwnership',), ('ConsumerOwnership.create',))
        _, c['registrar_token'] = seed_multi_authority(admin, pool, [('eios:action:' + n + ':1', ResourceType.ACTION, Operation.EXECUTE)
            for n in ('Consumer.create', 'ConsumerOwnership.create')], identity_suffix='-consumer-registrar', tenant=TENANT)
    session = authenticate_service(pool, c['registrar_token'], world='real')
    consumer = GovernedObjectCreator(pool, session, signer).create(action_name='Consumer.create', action_version=1,
        intent_id='webchat-consumer-' + secrets.token_hex(8), type_name='Consumer', properties={})['object_id']
    ConversationOwnershipRegistrar(pool, session, signer).register_consumer_owner(consumer_id=consumer, principal_id=human['principal_id'],
                                                                                  idempotency_key='webchat-owner-' + secrets.token_hex(8))
    return human, consumer, len(role['resources'])


def finish_erasure(c, admin, consumer):
    """The keeper runs the erasure to completion (the real completion: owner link deleted, contact keys recorded by 0358 D5)."""
    from multi_authority_fixture import seed_multi_authority
    from nexloop_eios import retention
    from nexloop_eios.authorization import authenticate_service
    if not c.get('erasure_keeper_token'):
        _, c['erasure_keeper_token'] = seed_multi_authority(admin, c['worker'], KEEPER, identity_suffix='-retention-keeper', tenant=TENANT)
    for _ in range(10):
        retention.RetentionKeeper(c['worker'], authenticate_service(c['worker'], c['erasure_keeper_token'], world='real'), c['signer'], host=lambda run_id: 'purged').run_once()
        if admin.execute('select state from control.nexloop_consumer_erasures where consumer_id=%s', (consumer,)).fetchone()[0] == 'completed':
            return admin.execute('select request_id,grants_cleared from control.nexloop_consumer_erasures where consumer_id=%s', (consumer,)).fetchone()
    raise AssertionError('erasure did not complete')


def erase_as_owner(c, consumer):
    with c['app']() as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        assert act(client, csrf, 'set_consumer_persona', {'consumer_id': consumer, 'persona': 'synthetic', 'reason': '合成'}).status_code == 200
        return act(client, csrf, 'erase_synthetic_consumer', {'consumer_id': consumer, 'reason': '合成删除'})


def owner_precheck(c, consumer):
    with c['app']() as client:
        workbench_login(client, c['owner_member'])
        return precheck(client, consumer).json()


def post_check(c, consumer, tmp_path, capsys, *extra):
    cfg = private_text(tmp_path, 'erase-' + secrets.token_hex(4) + '.json', json.dumps(
        {'schema': T.ERASE_SCHEMA, 'confirmation_ref': 'at050-erase:grants-1', 'reason': '合成顾客删除', 'consumers': [consumer]}))
    user = private_text(tmp_path, 'user-' + secrets.token_hex(4), c['owner_member']['username'] + '\n')
    with c['app']() as client:
        transport = AppTransport(client)
        code = T.main(['erase', '--origin', ORIGIN, '--password-stdin', '--username-file', str(user), '--config', str(cfg), *extra],
                      stdin=io.StringIO(c['owner_member']['password'] + '\n'), transport=transport)
    return code, json.loads(capsys.readouterr().out)['consumers'][0], transport.requests


@PLAN
def test_after_a_real_erasure_a_webchat_principal_is_untraceable(restoring, admin):
    """The evidence for 0429's design: a WebChat customer built and owned as on stage, erased through the real completion. The owner
    link is gone and no contact key of the request matches the principal's browser identity references (the Consumer never carried
    one: no identity link), so after the erasure nothing traces the principal; the precheck can only rely on the marker set at
    erasure time, and without it reports untraceable (null), never zero."""
    c = restoring
    publish_registry_actions(admin, ACTIONS)
    human, consumer, _ = webchat_customer(c, admin, 'traced-customer', grants='revoked')
    assert admin.execute('select count(*) from control.nexloop_consumer_owners where consumer_id=%s', (consumer,)).fetchone() == (1,)
    assert erase_as_owner(c, consumer).status_code == 200
    request_id, cleared = finish_erasure(c, admin, consumer)
    assert admin.execute('select count(*) from control.nexloop_consumer_owners where consumer_id=%s', (consumer,)).fetchone() == (0,)
    matched = admin.execute("""select count(*) from unnest(control.nexloop_principal_identity_refs(%s,'real',%s)) ref, control.nexloop_erasure_salts s
        join control.nexloop_erased_contact_keys x on x.tenant_id=s.tenant_id where s.tenant_id=%s and x.request_id=%s
        and x.key_digest=encode(extensions.hmac(convert_to(ref,'UTF8'),s.salt,'sha256'),'hex')""", (TENANT, human['principal_id'], TENANT, request_id)).fetchone()[0]
    keys = admin.execute('select count(*) from control.nexloop_erased_contact_keys where request_id=%s', (request_id,)).fetchone()[0]
    assert (matched, keys) == (0, 0)
    g = owner_sql(admin, "select control.nexloop_consumer_webchat_grants(%s,'real',%s,%s)", (TENANT, consumer, request_id))
    assert g['principals'] == 0 and g['via'] == 'contact_keys', g
    held = owner_precheck(c, consumer)['held']
    assert cleared is True and (held['grants_held'], held['grants_trace']) == (0, 'cleared_at_erasure'), held
    with admin.transaction():  # an erasure registered before 0429 (or by another path) carries no marker
        admin.execute('set local session_replication_role=replica')
        admin.execute('update control.nexloop_consumer_erasures set grants_cleared=null where request_id=%s', (request_id,))
    held = owner_precheck(c, consumer)['held']
    assert (held['grants_held'], held['grants_trace']) == (None, 'untraceable'), held


@PLAN
def test_the_erasure_is_refused_while_the_principal_holds_grants_and_passes_once_revoked(restoring, admin, tmp_path, capsys):
    """Revoke, erase, verify (all real): with grants the tool does not send and SQL refuses (409 grants_held, nothing registered);
    after webchat_consumers' revoke the erasure runs, the keeper completes it and the post-check passes on the marker."""
    from nexloop_eios import webchat_consumers as W
    from test_workbench_read_pg import write_facts
    c = restoring
    publish_registry_actions(admin, ACTIONS)
    human, consumer, n = webchat_customer(c, admin, 'gated-customer')
    with c['app']() as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        assert act(client, csrf, 'set_consumer_persona', {'consumer_id': consumer, 'persona': 'synthetic', 'reason': '合成'}).status_code == 200
        held = precheck(client, consumer).json()['held']
        assert (held['grants_held'], held['grants_trace']) == (n, 'owner_link'), held
    code, result, requests = post_check(c, consumer, tmp_path, capsys)
    assert (code, result['result']) == (1, 'grants_held_revoke_first') and not any(m == 'POST' and '/actions/' in p for m, p in requests)
    with c['app']() as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        refused = act(client, csrf, 'erase_synthetic_consumer', {'consumer_id': consumer, 'reason': '合成删除'})
    assert refused.status_code == 409 and refused.json()['details'] == {'reason': 'grants_held'}, refused.text
    assert admin.execute('select count(*) from control.nexloop_consumer_erasures where consumer_id=%s', (consumer,)).fetchone() == (0,)
    role = json.loads(ROLE_FILE.read_text())
    write_facts(admin, W.compile_recipe(role, {'schema_version': W.CONSUMERS_SCHEMA, 'tenant_id': TENANT, 'browser_application_id': 'synthetic-webchat',
        'caller_application_id': 'eios:application:nexloop-webchat', 'consumers': [{'principal_id': human['principal_id'], 'subject_id': human['subject_id'],
        'persona': 'simulated_agent', 'revoke': True}]})['authority_facts'])
    code, result, _ = post_check(c, consumer, tmp_path, capsys)
    assert (code, result['result']) == (0, 'erasing'), result
    _, cleared = finish_erasure(c, admin, consumer)
    code, result, _ = post_check(c, consumer, tmp_path, capsys, '--dry-run')
    assert cleared is True and (code, result['result'], result['before']['held']['grants_held'], result['before']['held']['grants_trace']) == (0, 'already_erased', 0, 'cleared_at_erasure'), result


@PLAN
def test_an_erased_consumer_without_the_marker_or_without_any_grant_fact_fails_the_post_check(restoring, admin, tmp_path, capsys):
    """Fail closed. (a) An erasure without the marker (registered before 0429, as customer-4) is untraceable afterwards:
    erased_grants_unknown, exit 1, even with no grants left anywhere. (b) Empty configuration: the principal has no grants fact at
    all (never configured, so nothing shows a revocation): the erasure is registered with grants_cleared false and the post-check
    is erased_grants_unknown, exit 1, never zero."""
    c = restoring
    publish_registry_actions(admin, ACTIONS)
    _, legacy, _ = webchat_customer(c, admin, 'legacy-customer', grants='revoked')
    assert erase_as_owner(c, legacy).status_code == 200
    request_id, _ = finish_erasure(c, admin, legacy)
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute('update control.nexloop_consumer_erasures set grants_cleared=null where request_id=%s', (request_id,))
    code, result, _ = post_check(c, legacy, tmp_path, capsys, '--dry-run')
    assert (code, result['result'], result['before']['held']['grants_held'], result['before']['held']['grants_trace']) == (1, 'erased_grants_unknown', None, 'untraceable'), result
    _, bare, _ = webchat_customer(c, admin, 'unconfigured-customer', grants='none')
    held = owner_precheck(c, bare)['held']
    assert (held['grants_held'], held['grants_trace']) == (None, 'no_grant_facts'), held
    assert erase_as_owner(c, bare).status_code == 200  # nothing is held, so not refused; but nothing proves a revocation either
    _, cleared = finish_erasure(c, admin, bare)
    code, result, _ = post_check(c, bare, tmp_path, capsys, '--dry-run')
    assert cleared is False and (code, result['result'], result['before']['held']['grants_held']) == (1, 'erased_grants_unknown', None), result


def _legacy_owner_row(admin, consumer, principal, key):
    """An erasure completed before 0306 (stage customer-4, 2026-10-19): the owner row was never deleted and the 0429 marker is null.
    Recreated with triggers off (session_replication_role replica), the state 0306/0442 found on stage."""
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("insert into control.nexloop_consumer_owners values(%s,'real',%s,%s,%s,%s)", (TENANT, principal, consumer, 'ownership-' + key, key))
        admin.execute('update control.nexloop_consumer_erasures set grants_cleared=null where consumer_id=%s', (consumer,))


def _owner_rows(admin, consumer):
    return admin.execute('select count(*) from control.nexloop_consumer_owners where consumer_id=%s', (consumer,)).fetchone()[0]


@PLAN
def test_a_completed_erasure_leaves_no_owner_link_and_none_can_be_added_afterwards(restoring, admin):
    """0442 guard: erased => no owner row. After the real completion the link is gone, and neither the governed registrar (the Consumer
    object is gone) nor a direct insert as the owner role (the trigger) puts one back."""
    c = restoring
    publish_registry_actions(admin, ACTIONS)
    human, consumer, _ = webchat_customer(c, admin, 'guarded-customer', grants='revoked')
    assert erase_as_owner(c, consumer).status_code == 200
    finish_erasure(c, admin, consumer)
    assert _owner_rows(admin, consumer) == 0
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='consumer erased'):
        owner_sql(admin, "insert into control.nexloop_consumer_owners values(%s,'real',%s,%s,'rebound','rebound-key') returning 1",
                  (TENANT, human['principal_id'], consumer))
    assert _owner_rows(admin, consumer) == 0
    # Every completed erasure in the database: no owner row (the invariant the settle function restores for older erasures).
    assert admin.execute("""select count(*) from control.nexloop_consumer_erasures e join control.nexloop_consumer_owners o
        on o.tenant_id=e.tenant_id and o.world=e.world and o.consumer_id=e.consumer_id where e.state='completed'""").fetchone() == (0,)


@PLAN
def test_the_residue_of_an_erasure_completed_before_0306_is_settled_and_the_trace_becomes_cleared_at_erasure(restoring, admin, tmp_path, capsys):
    """Stage customer-4: erased under 0221, owner row left, no marker. Precheck: grants_held 0 via owner_link. The 0442 settle function
    sets the marker from the revoked grants and deletes the row; the precheck then reports cleared_at_erasure and the post-check passes.
    With grants still held the row stays (and the precheck keeps showing them), so the owner revokes first; a rerun settles nothing new."""
    from nexloop_eios import webchat_consumers as W
    from test_workbench_read_pg import write_facts
    c = restoring
    publish_registry_actions(admin, ACTIONS)
    human, consumer, n = webchat_customer(c, admin, 'residue-customer', grants='revoked')
    assert erase_as_owner(c, consumer).status_code == 200
    finish_erasure(c, admin, consumer)
    _legacy_owner_row(admin, consumer, human['principal_id'], 'business-setup-legacy-ownership')
    held = owner_precheck(c, consumer)['held']
    assert (held['grants_held'], held['grants_trace']) == (0, 'owner_link'), held
    role = json.loads(ROLE_FILE.read_text())
    grant = W.compile_recipe(role, {'schema_version': W.CONSUMERS_SCHEMA, 'tenant_id': TENANT, 'browser_application_id': 'synthetic-webchat',
        'caller_application_id': 'eios:application:nexloop-webchat', 'consumers': [{'principal_id': human['principal_id'], 'subject_id': human['subject_id'],
        'persona': 'simulated_agent'}]})['authority_facts']
    write_facts(admin, grant)  # granted again: the settle function keeps the row
    assert owner_sql(admin, 'select control.nexloop_erasure_owner_residue_settle(%s)', (TENANT,)) == {'settled': 0, 'kept_grants_held': 1}
    assert _owner_rows(admin, consumer) == 1 and owner_precheck(c, consumer)['held']['grants_held'] == n
    write_facts(admin, W.compile_recipe(role, {'schema_version': W.CONSUMERS_SCHEMA, 'tenant_id': TENANT, 'browser_application_id': 'synthetic-webchat',
        'caller_application_id': 'eios:application:nexloop-webchat', 'consumers': [{'principal_id': human['principal_id'], 'subject_id': human['subject_id'],
        'persona': 'simulated_agent', 'revoke': True}]})['authority_facts'])
    assert owner_sql(admin, 'select control.nexloop_erasure_owner_residue_settle(%s)', (TENANT,)) == {'settled': 1, 'kept_grants_held': 0}
    assert _owner_rows(admin, consumer) == 0
    assert admin.execute('select grants_cleared from control.nexloop_consumer_erasures where consumer_id=%s', (consumer,)).fetchone() == (True,)
    code, result, _ = post_check(c, consumer, tmp_path, capsys, '--dry-run')
    assert (code, result['result'], result['before']['held']['grants_held'], result['before']['held']['grants_trace']) == (0, 'already_erased', 0, 'cleared_at_erasure'), result
    assert owner_sql(admin, 'select control.nexloop_erasure_owner_residue_settle(%s)', (TENANT,)) == {'settled': 0, 'kept_grants_held': 0}
