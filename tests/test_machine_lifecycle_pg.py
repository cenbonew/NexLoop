"""NX-062 M2b (docs/implementation/NX-062-design.md §1.4, §2.2; ADR-041 Q-4; migration nx062_machine_lifecycle) on real PostgreSQL and
the real workbench HTTP entry: the owner registers a machine client, issues a key (shown once, only its HMAC digest stored), rotates it
within the overlap, revokes it, deactivates the client (every key revoked) and activates it again (no key revived). An operator is refused;
the handler itself refuses a principal that is not an active owner even with the grant; refusals are fixed codes; the body cannot carry
key fields or identity; a replay never shows the key again; the audit rows (hash chain table) carry ids and prefixes, never the key or
its digest. Keys are generated at run time; no key literal appears here."""
import dataclasses
import secrets

import psycopg
import pytest
from fastapi.testclient import TestClient

from commercial_fixture import publish_registry_actions
from nexloop_eios import machine_keys as K
from nexloop_eios.http_api import create_app
from support.tenants import TENANT_A as TENANT
from test_workbench_actions_http import act
from test_workbench_read_pg import (ORIGIN, browser_business, conversations, identity, provision, published_action, uow, workbench,  # noqa: F401
                                    workbench_login)

ACTIONS = tuple('nexloop.' + c for c in ('machine_client.register', 'machine_client.deactivate', 'machine_client.activate',
                                         'machine_key.issue', 'machine_key.rotate', 'machine_key.revoke'))
VERB = 'object.get'
MIGRATION = 'migration:0434_nx062_machine_access.sql'


@pytest.fixture
def desk(workbench, tmp_path):
    w = workbench
    admin = w['admin']
    publish_registry_actions(admin, ACTIONS, tenant=TENANT)
    provision(w)
    admin.execute("insert into control.nexloop_machine_read_verbs(verb,projection,description,source_task) values(%s,'[\"object_id\"]','synthetic read','NX-062')", (VERB,))
    admin.execute("insert into control.nexloop_machine_capabilities(capability,revision,state,kind,description,source,recorded_by) values(%s,1,'active','read','synthetic',%s,'test')",
                  (VERB, MIGRATION))
    peppers = tmp_path / 'machine-key-peppers'
    peppers.write_text(f'1 {secrets.token_hex(32)}\n')
    peppers.chmod(0o600)
    config = dataclasses.replace(w['config'], workbench=dataclasses.replace(w['config'].workbench, machine_key_peppers_file=peppers))
    w = {**w, 'peppers': K.load_peppers(peppers), 'peppers_file': peppers, 'client': lambda: TestClient(create_app(config), base_url=ORIGIN)}
    return w


def snapshot(admin, token, peppers):
    key = K.parse(token)
    try:
        with admin.transaction():
            return admin.execute('select authz.nexloop_machine_identity_snapshot(%s,%s)', (key.prefix_id, K.digest(key, peppers, 1).hex())).fetchone()[0]
    except psycopg.errors.InsufficientPrivilege:
        return None


def owner(client, w):
    return workbench_login(client, w['owner']).json()['csrf_token']


def test_owner_lifecycle_end_to_end_over_http(desk):
    w = desk
    admin = w['admin']
    with w['client']() as client:
        csrf = owner(client, w)
        r = act(client, csrf, 'register_machine_client', {'client_id': 'acme-agent', 'display_name': '客服 Agent', 'purpose': '同步工单状态'})
        assert r.status_code == 200, r.text
        assert r.json()['principal_id'] == 'nexloop-machine:acme-agent'
        again = act(client, csrf, 'register_machine_client', {'client_id': 'acme-agent', 'display_name': 'x', 'purpose': 'y'})
        assert again.status_code == 409 and again.json()['details'] == {'reason': 'client_exists'}
        issued = act(client, csrf, 'issue_machine_key', {'client_id': 'acme-agent', 'world': 'test', 'scope': [VERB], 'expires_in_days': 30},
                     key='machine-issue-test-0001')
        assert issued.status_code == 200, issued.text
        body = issued.json()
        assert issued.headers['cache-control'] == 'no-store' and body['replayed'] is False
        token = body['key']
        assert K.parse(token).prefix_id == body['key_prefix'] and body['world'] == 'test' and body['scope'] == [VERB]
        # Only the digest is stored; the key is nowhere in the database, not in the audit.
        dump = admin.execute("""select string_agg(t,' ') from (select to_jsonb(k)::text t from authz.nexloop_machine_keys k
            union all select to_jsonb(a)::text from runtime.audit_events a) x""").fetchone()[0]
        assert token not in dump and K.parse(token).secret not in dump
        audit = admin.execute("select payload from runtime.audit_events where action='nexloop.machine_key.issue'").fetchone()[0]
        assert audit['key_prefix'] == body['key_prefix'] and 'digest' not in audit
        assert K.digest(K.parse(token), w['peppers'], 1).hex() not in admin.execute('select string_agg(payload::text,\' \') from runtime.audit_events').fetchone()[0]
        assert snapshot(admin, token, w['peppers'])['binding']['subject_principal_id'] == 'nexloop-machine:acme-agent'
        # A retry of the same request draws a new key, so its body differs from the claimed one: refused (409), nothing issued, the first
        # key never shown again (the invitation entry behaves the same; an owner retries with a new request and revokes a lost key).
        replay = act(client, csrf, 'issue_machine_key', {'client_id': 'acme-agent', 'world': 'test', 'scope': [VERB], 'expires_in_days': 30},
                     key='machine-issue-test-0001')
        assert replay.status_code == 409 and 'key' not in replay.json(), replay.text
        assert token[17:] not in replay.text
        assert admin.execute('select count(*) from authz.nexloop_machine_keys').fetchone()[0] == 1
        # A second plain issue is refused: rotation is the way to a second key.
        second = act(client, csrf, 'issue_machine_key', {'client_id': 'acme-agent', 'world': 'test', 'scope': [VERB], 'expires_in_days': 30})
        assert second.status_code == 409 and second.json()['details'] == {'reason': 'rotate_required'}
        rotated = act(client, csrf, 'rotate_machine_key', {'previous_key_id': body['key_id'], 'overlap_days': 7, 'scope': [VERB], 'expires_in_days': 60})
        assert rotated.status_code == 200, rotated.text
        new = rotated.json()['key']
        assert snapshot(admin, token, w['peppers']) and snapshot(admin, new, w['peppers'])
        third = act(client, csrf, 'rotate_machine_key', {'previous_key_id': rotated.json()['key_id'], 'overlap_days': 1, 'scope': [VERB], 'expires_in_days': 10})
        assert third.status_code == 409 and third.json()['details'] == {'reason': 'key_limit'}
        revoked = act(client, csrf, 'revoke_machine_key', {'key_id': body['key_id'], 'reason': '轮换完成'})
        assert revoked.status_code == 200 and revoked.json()['changed'] is True and snapshot(admin, token, w['peppers']) is None
        assert act(client, csrf, 'revoke_machine_key', {'key_id': body['key_id'], 'reason': '再来一次'}).json()['changed'] is False
        off = act(client, csrf, 'deactivate_machine_client', {'client_id': 'acme-agent', 'reason': '停用'})
        assert off.status_code == 200 and off.json()['keys_revoked'] == 1 and snapshot(admin, new, w['peppers']) is None
        refused = act(client, csrf, 'rotate_machine_key', {'previous_key_id': rotated.json()['key_id'], 'overlap_days': 0, 'scope': [VERB], 'expires_in_days': 10})
        assert refused.status_code == 409 and refused.json()['details'] == {'reason': 'client_inactive'}
        on = act(client, csrf, 'activate_machine_client', {'client_id': 'acme-agent', 'reason': '恢复'})
        assert on.status_code == 200 and on.json()['changed'] is True and snapshot(admin, new, w['peppers']) is None
    actions = [r[0] for r in admin.execute("select action from runtime.audit_events where trace_id='machine_access' order by created_at").fetchall()]
    assert actions == ['nexloop.machine_client.register', 'nexloop.machine_key.issue', 'nexloop.machine_key.rotate', 'nexloop.machine_key.revoke',
                       'nexloop.machine_client.deactivate', 'nexloop.machine_client.activate']
    assert {r[0] for r in admin.execute("select actor_id from runtime.audit_events where trace_id='machine_access'")} == {w['owner']['principal_id']}


def test_refusals_are_fixed_codes_and_the_body_is_closed(desk):
    w = desk
    with w['client']() as client:
        csrf = owner(client, w)
        assert act(client, csrf, 'register_machine_client', {'client_id': 'acme-agent', 'display_name': 'A', 'purpose': 'p'}).status_code == 200
        for operation, body in (
                ('register_machine_client', {'client_id': 'Acme', 'display_name': 'A', 'purpose': 'p'}),
                ('register_machine_client', {'client_id': 'acme-two', 'display_name': 'A', 'purpose': 'p', 'tenant_id': TENANT}),
                ('issue_machine_key', {'client_id': 'acme-agent', 'world': 'test', 'scope': [VERB], 'expires_in_days': 91}),
                ('issue_machine_key', {'client_id': 'acme-agent', 'world': 'test', 'scope': [VERB], 'expires_in_days': 30, 'digest': '0' * 64}),
                ('issue_machine_key', {'client_id': 'acme-agent', 'world': 'test', 'scope': [VERB], 'expires_in_days': 30, 'prefix_id': 'a' * 12}),
                ('issue_machine_key', {'client_id': 'acme-agent', 'world': 'sim:x', 'scope': [VERB], 'expires_in_days': 30}),
                ('issue_machine_key', {'client_id': 'acme-agent', 'world': 'test', 'scope': [], 'expires_in_days': 30}),
                ('issue_machine_key', {'client_id': 'acme-agent', 'world': 'test', 'scope': [VERB], 'expires_in_days': True}),
                ('rotate_machine_key', {'previous_key_id': 'x', 'overlap_days': 8, 'scope': [VERB], 'expires_in_days': 1})):
            assert act(client, csrf, operation, body).status_code == 422, (operation, body)
        scope = act(client, csrf, 'issue_machine_key', {'client_id': 'acme-agent', 'world': 'test', 'scope': ['object.list'], 'expires_in_days': 30})
        assert scope.status_code == 409 and scope.json()['details'] == {'reason': 'scope_not_allowed'}
        never = act(client, csrf, 'issue_machine_key', {'client_id': 'acme-agent', 'world': 'test', 'scope': ['approval.decide'], 'expires_in_days': 30})
        assert never.status_code == 409 and never.json()['details'] == {'reason': 'scope_not_allowed'}
        missing = act(client, csrf, 'issue_machine_key', {'client_id': 'nobody-here', 'world': 'test', 'scope': [VERB], 'expires_in_days': 30})
        assert missing.status_code == 409 and missing.json()['details'] == {'reason': 'client_not_found'}
        assert act(client, csrf, 'revoke_machine_key', {'key_id': '00000000-0000-4000-8000-000000000000', 'reason': 'x'}).json()['details'] == {'reason': 'key_not_found'}


def test_operator_refused_and_the_handler_requires_an_active_owner(desk):
    w = desk
    admin = w['admin']
    with w['client']() as client:
        csrf = workbench_login(client, w['operator']).json()['csrf_token']
        assert act(client, csrf, 'register_machine_client', {'client_id': 'acme-agent', 'display_name': 'A', 'purpose': 'p'}).status_code == 403
    assert admin.execute('select count(*) from authz.nexloop_machine_clients').fetchone()[0] == 0
    # The handler, called as the entry would with a principal that holds no owner role (or a machine principal), refuses on its own.
    body = '{"operation":"register_machine_client","request_id":"r1","client_id":"acme-agent","display_name":"A","purpose":"p"}'
    for principal, subject in ((w['operator']['principal_id'], 'operator-subject'), ('nexloop-machine:acme-agent', 'nexloop-machine-subject:acme-agent')):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='owner_required'):
            with admin.transaction():
                admin.execute('select control.nexloop_governed_register_machine_client(%s,%s,%s,%s,%s,%s::jsonb)', (TENANT, 'real', principal, subject, 'i1', body))
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='owner_world_only'):
        with admin.transaction():
            admin.execute('select control.nexloop_governed_register_machine_client(%s,%s,%s,%s,%s,%s::jsonb)', (TENANT, 'test', w['owner']['principal_id'], 's', 'i1', body))
    # The handler checks the key world itself (the HTTP spec refuses it first): only real or test, never a simulation or shadow world.
    owner_p = w['owner']['principal_id']
    admin.execute('select control.nexloop_governed_register_machine_client(%s,%s,%s,%s,%s,%s::jsonb)', (TENANT, 'real', owner_p, 's', 'i2', body))
    for world in ('sim:x', 'shadow:y'):
        issue = ('{"operation":"issue_machine_key","request_id":"r2","client_id":"acme-agent","world":"%s","scope":["object.get"],"expires_in_days":5,'
                 '"prefix_id":"%s","digest":"%s","pepper_version":1}') % (world, K.generate().prefix_id, secrets.token_hex(32))
        with pytest.raises(psycopg.errors.InvalidParameterValue, match='world_not_allowed'):
            with admin.transaction():
                admin.execute('select control.nexloop_governed_issue_machine_key(%s,%s,%s,%s,%s,%s::jsonb)', (TENANT, 'real', owner_p, 's', 'i3', issue))
    # No application role may call a handler directly; all six are human-only registry rows.
    rows = admin.execute("select capability,subject_rule,source_task from control.nexloop_governed_capabilities where capability like 'machine\\_%' order by 1").fetchall()
    assert rows == [(c, 'human', 'NX-062') for c in sorted(a.removeprefix('nexloop.') for a in ACTIONS)]
    for (handler,) in admin.execute("select distinct handler::text from control.nexloop_governed_capabilities where capability like 'machine\\_%'").fetchall():
        for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_action_worker', 'nexloop_scheduler', 'nexloop_runtime', 'nexloop_identity', 'nexloop_configurator'):
            assert not admin.execute("select has_function_privilege(%s,%s,'execute')", (role, handler)).fetchone()[0], (role, handler)


def test_issue_without_a_pepper_file_is_unavailable(desk):
    w = desk
    w['peppers_file'].chmod(0o644)  # not private: the file is refused, nothing is issued
    with w['client']() as client:
        csrf = owner(client, w)
        assert act(client, csrf, 'register_machine_client', {'client_id': 'acme-agent', 'display_name': 'A', 'purpose': 'p'}).status_code == 200
        r = act(client, csrf, 'issue_machine_key', {'client_id': 'acme-agent', 'world': 'test', 'scope': [VERB], 'expires_in_days': 30})
        assert r.status_code == 503 and 'key' not in r.json()
    assert w['admin'].execute('select count(*) from authz.nexloop_machine_keys').fetchone()[0] == 0


SAMPLE = {'text': "'x'", 'jsonb': "'{}'::jsonb", 'integer': '1', 'bigint': '1', 'uuid': "'00000000-0000-4000-8000-000000000001'::uuid",
          'timestamp with time zone': 'clock_timestamp()', 'text[]': "array['x']", 'boolean': 'false'}
# The one digest entry that answers an unknown digest with an empty list instead of raising; a machine digest gets the same empty answer.
EMPTY_FOR_UNKNOWN = {'authz.nexloop_definition_read(text,text,text,text[])'}


def test_until_m5_a_machine_key_reaches_no_governed_entry_and_holds_no_fact(desk):
    """Dispatcher 2026-11-09: after M2b and before M5 a machine principal has no authority fact and no usable read or write. Every function
    nexloop_api may call whose first argument is a token digest refuses a live machine key's digest (or, like for an unknown digest, returns
    nothing); the sweep fails if it finds fewer than 100 such entries, so it can never pass empty."""
    w = desk
    admin = w['admin']
    with w['client']() as client:
        csrf = owner(client, w)
        assert act(client, csrf, 'register_machine_client', {'client_id': 'acme-agent', 'display_name': 'A', 'purpose': 'p'}).status_code == 200
        token = act(client, csrf, 'issue_machine_key', {'client_id': 'acme-agent', 'world': 'real', 'scope': [VERB], 'expires_in_days': 30}).json()['key']
    assert snapshot(admin, token, w['peppers'])  # the key is live
    digest = K.digest(K.parse(token), w['peppers'], 1).hex()
    # No authority fact, no grant, no workbench grant instance names the machine principal.
    assert admin.execute("select count(*) from authz.nexloop_authority_facts where payload::text like '%%nexloop-machine%%' or entity_key::text like '%%nexloop-machine%%'").fetchone()[0] == 0
    assert admin.execute("select count(*) from control.nexloop_workbench_grant_instances where principal_id like 'nexloop-machine:%%'").fetchone()[0] == 0
    # No HTTP route takes a machine key yet (the machine_key realm arrives with M3).
    import json
    from pathlib import Path
    inventory = json.loads((Path(__file__).resolve().parents[1] / 'deploy/authorization/api-routes.v1.json').read_text())
    assert 'machine_key' not in {r['realm'] for r in inventory['routes']}
    rows = admin.execute("""select p.oid::regprocedure::text,p.oid::regproc::text,array(select format_type(t,null) from unnest(p.proargtypes) t)
        from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname in ('authz','control','runtime')
        and has_function_privilege('nexloop_api',p.oid,'execute') and p.pronargs>=1 and p.proargtypes[0]='text'::regtype
        and p.proargnames[1] ~ 'digest|token'""").fetchall()
    assert len(rows) >= 100, len(rows)
    answered = {}
    admin.execute('set role nexloop_api')
    try:
        for signature, name, types in rows:
            args = ['%s'] + [("'real'" if i == 1 and t == 'text' else SAMPLE[t]) for i, t in enumerate(types) if i > 0]
            try:
                with admin.transaction():
                    answered[signature] = admin.execute(f"select {name}({','.join(args)})", (digest,)).fetchone()[0]
            except psycopg.Error:
                pass
    finally:
        admin.execute('reset role')
    assert set(answered) <= EMPTY_FOR_UNKNOWN and all(v in (None, [], {}) for v in answered.values()), answered
