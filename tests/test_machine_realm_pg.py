"""NX-062 M3 (docs/implementation/NX-062-design.md §2.1, §3.1, §3.5; ADR-041 §2.4-2.5, §2.7; L6 C1, C2, C4) on real PostgreSQL and the
real HTTP application: the machine_key realm.

* G-5 extended to credentials (C1 at HTTP): every machine_key route refuses no credential, another Bearer value, a workbench cookie session
  and a revoked machine key (401); a live machine key is no credential on any other inventoried route (401/403). The matrix fails when it
  is empty or its size is not machine routes x 4 + other routes.
* Admission (C4): without a published rate policy every request is refused (503 rate_policy_missing) and counted, and the metrics block
  says so (the v35 rule fires); the policy is published by trusted configuration; per-key windows answer 429 with Retry-After; every
  outcome is counted; an unknown, revoked or expired key or one of a deactivated client is 401 and counted as denied.
* A query string is refused (400) before anything is read: a key never travels in a URL; no response repeats a key.
* Until M5 the capability list is empty, even for a key whose scope is allowlisted.
Keys are generated at run time; no key literal appears here."""
import dataclasses
import json
import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nexloop_eios import api_route_inventory as I
from nexloop_eios import machine_access as A
from nexloop_eios import machine_keys as K
from nexloop_eios.http_api import create_app
from nexloop_eios.observability import apply_rules, load_rules
from support.tenants import TENANT_A as TENANT
from nexloop_eios import commercial
from test_api_route_inventory_pg import BODIES, EXEMPT, PARAMETERS, WRITE_HEADERS, concrete
from test_machine_lifecycle_pg import VERB, desk, owner  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import ORIGIN, browser_business, conversations, identity, published_action, uow, workbench  # noqa: F401
from test_workbench_read_pg import workbench_login

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / 'deploy/configuration/machine-rate-limits.v1.json'


@pytest.fixture
def realm(desk, tmp_path):
    w = desk
    base = w['config']
    config = dataclasses.replace(base, machine_key_peppers_file=w['peppers_file'],
                                 workbench=dataclasses.replace(base.workbench, machine_key_peppers_file=w['peppers_file']))
    w = {**w, 'config': config, 'client': lambda mode='test': TestClient(create_app(dataclasses.replace(config, mode=mode)), base_url=ORIGIN)}
    with w['client']() as client:
        csrf = owner(client, w)
        assert act(client, csrf, 'register_machine_client', {'client_id': 'acme-agent', 'display_name': 'A', 'purpose': 'p'}).status_code == 200
        issued = act(client, csrf, 'issue_machine_key', {'client_id': 'acme-agent', 'world': 'real', 'scope': [VERB], 'expires_in_days': 30}).json()
    return {**w, 'key': issued['key'], 'key_id': issued['key_id'], 'csrf': csrf}


def publish(w, path=POLICY, **override):
    policy = {**json.loads(path.read_text()), **override}
    target = Path(w['peppers_file']).parent / 'policy.json'
    target.write_text(json.dumps(policy))
    return A.publish_rate_policy(target, database_url_file=w['configurator'])


def metrics(w):
    return w['admin'].execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'machine_access'", (TENANT,)).fetchone()[0]


def counts(w):
    return dict(w['admin'].execute("select outcome,sum(calls)::int from runtime.nexloop_machine_call_counts group by outcome").fetchall())


def valid(response, success='machine-capabilities'):
    """The response body against its published contract (packages/contracts machine-capabilities or machine-error)."""
    from jsonschema import Draft202012Validator, FormatChecker
    name = success if response.status_code == 200 else 'machine-error'
    schema = json.loads((ROOT / f'packages/contracts/{name}.schema.json').read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(response.json())
    return response


def bearer(token):
    return {'Authorization': 'Bearer ' + token}


KINDS = ('none', 'machine_key', 'workbench_cookie', 'consumer_cookie', 'service_token', 'channel_signature')


def test_g5_credential_matrix_machine_realm_is_exclusive(realm):
    """L6 H1: every machine_key route x the six credential kinds of the design, each cell asserted. Only a live machine key passes
    (200); no credential, a workbench cookie session, a consumer (WebChat) cookie session, a real service token that authenticates as a
    service principal, and a validly signed channel (connector) request are all 401. Every other inventoried route refuses a live machine
    key (401/403). The matrix fails when empty or when its machine part is not routes x 6."""
    from eios.authz.operations import Operation
    from eios.authz.resources import ResourceType
    from multi_authority_fixture import seed_multi_authority
    from nexloop_eios.authorization import authenticate_service
    w = realm
    publish(w)
    # A configured connector, so a signed channel request is a real channel credential and the webhook answers its signature check.
    key = Path(w['peppers_file']).parent / 'probe-connector-key'
    key.write_text(secrets.token_hex(32))
    key.chmod(0o600)
    commercial.configure_connector(w['configurator'], tenant=TENANT, world='real', connector_id=PARAMETERS['connector_id'], data_mode='real',
                                   event_types=['order.paid'], currencies=['CNY'], key_id='k1', key_file=key, owner_confirmation='synthetic owner confirmation')
    # A real service token: it authenticates as a service principal through the service realm.
    reader = w['f']['reader']
    _, service_token = seed_multi_authority(w['admin'], reader.pool, [('eios:action:Consumer.create:1', ResourceType.ACTION, Operation.EXECUTE)],
                                            identity_suffix='-g5-machine-probe')
    assert authenticate_service(reader.pool, service_token, world='real').authentication.subject_kind.value == 'service'
    stamp = str(int(__import__('time').time()))
    signed = {'X-NexLoop-Timestamp': stamp, 'X-NexLoop-Signature': commercial.sign(bytes.fromhex(key.read_text()), stamp, b'')}
    inventory = I.load()
    routes = [r for r in I.listed(inventory, 'test') if r['realm'] not in EXEMPT]
    machine = [r for r in routes if r['realm'] == 'machine_key']
    other = [r for r in routes if r['realm'] != 'machine_key']
    assert machine and other
    live = w['key']
    cells = {}

    def send(client, r, headers=None):
        path = concrete(r['path'])
        if r['method'] == 'GET':
            return client.get(path, headers=headers or {}).status_code
        return client.request(r['method'], path, json=BODIES.get(r['path'], {}), headers={**WRITE_HEADERS, **(headers or {})}).status_code

    for r in machine:
        with w['client']() as client:
            cells[(r['path'], 'none')] = send(client, r)
            cells[(r['path'], 'machine_key')] = send(client, r, bearer(live))
            cells[(r['path'], 'service_token')] = send(client, r, bearer(service_token))
            cells[(r['path'], 'channel_signature')] = send(client, r, signed)
        with w['client']() as client:
            assert workbench_login(client, w['owner']).status_code == 200
            cells[(r['path'], 'workbench_cookie')] = send(client, r)
        with w['client']() as client:
            login = client.post('/api/v1/auth/login', headers={'Origin': ORIGIN}, json={'username': w['customer']['username'], 'password': w['customer']['password']})
            assert login.status_code == 200, login.text
            cells[(r['path'], 'consumer_cookie')] = send(client, r)
    assert len(cells) == len(machine) * len(KINDS) and set(k for _, k in cells) == set(KINDS)
    for r in machine:
        # The live key passes authentication: 200, or the route's own answer for an ungranted capability or absent object or intent (403/404).
        assert cells[(r['path'], 'machine_key')] in ((200,) if r['path'] == '/machine/v1/capabilities' else (200, 403, 404)), (r['path'], cells[(r['path'], 'machine_key')])
        assert cells[(r['path'], 'none')] == 401, r['path']
        assert cells[(r['path'], 'workbench_cookie')] == 401, r['path']
        assert cells[(r['path'], 'consumer_cookie')] == 401, r['path']
        assert cells[(r['path'], 'service_token')] == 401, r['path']
        assert cells[(r['path'], 'channel_signature')] == 401, r['path']
    refused = {}
    with w['client']() as client:
        for r in other:
            refused[(r['method'], r['path'])] = send(client, r, bearer(live))
    assert len(refused) == len(other) > 0
    assert {k: v for k, v in refused.items() if v not in (401, 403)} == {}


def test_c4_no_policy_fails_closed_and_is_counted_then_the_policy_limits(realm):
    w = realm
    assert metrics(w)['rate_policy_missing'] == 1
    v35 = load_rules(ROOT / 'deploy/configuration/alert-rules.v35.json')
    rule = next(r for r in v35['rules'] if r['rule_id'] == 'machine_rate_policy_missing')
    assert rule['metric'] == 'machine_access.rate_policy_missing' and rule['threshold'] == 0 and rule['severity'] == 'critical'
    assert apply_rules(v35, TENANT, database_url_file=w['configurator'])['configured'] is True  # the SQL configurator accepts v35
    with w['client']() as client:
        refused = valid(client.get('/machine/v1/capabilities', headers=bearer(w['key'])))
        assert refused.status_code == 503 and refused.json()['code'] == 'rate_policy_missing' and w['key'] not in refused.text
    assert counts(w) == {'rate_policy_missing': 1} and metrics(w)['refused_policy_missing_24h'] == 1
    assert metrics(w)['rate_policy_missing'] > rule['threshold']  # the v35 rule fires
    first = publish(w, per_key_per_minute=3)
    assert first['changed'] is True and publish(w, per_key_per_minute=3)['changed'] is False
    assert metrics(w)['rate_policy_missing'] == 0
    with w['client']() as client:
        for _ in range(3):
            ok = valid(client.get('/machine/v1/capabilities', headers=bearer(w['key'])))
            assert ok.status_code == 200 and ok.json() == {'capabilities': []} and ok.headers['cache-control'] == 'no-store'
        limited = valid(client.get('/machine/v1/capabilities', headers=bearer(w['key'])))
        assert limited.status_code == 429 and 1 <= int(limited.headers['retry-after']) <= 60
    assert counts(w) == {'rate_policy_missing': 1, 'admitted': 3, 'rate_limited': 1}
    assert metrics(w)['admitted_24h'] == 3 and metrics(w)['rate_limited_24h'] == 1
    # A lower manifest version is refused; only trusted configuration may publish.
    with pytest.raises(Exception):
        publish(w, manifest_version=1, per_key_per_minute=4)
    with pytest.raises(Exception, match='trusted configuration'):
        w['admin'].execute("select control.nexloop_publish_machine_rate_policy('{}'::jsonb)")


def test_denials_query_strings_and_the_empty_list_until_m5(realm):
    w = realm
    publish(w)
    with w['client']() as client:
        # The key in a URL is never accepted, and nothing is read or counted for it.
        for query in ('?key=' + w['key'], '?x=1'):
            r = valid(client.get('/machine/v1/capabilities' + query, headers=bearer(w['key'])))
            assert r.status_code == 400 and r.json()['code'] == 'query_not_allowed' and w['key'] not in r.text
        assert counts(w) == {}
        other = K.generate()
        unknown = valid(client.get('/machine/v1/capabilities', headers=bearer(other.token)))  # unknown prefix
        forged = client.get('/machine/v1/capabilities', headers=bearer(K.MachineKey(K.parse(w['key']).prefix_id, other.secret).token))
        assert unknown.status_code == forged.status_code == 401 and unknown.json()['code'] == forged.json()['code'] == 'unauthenticated'
        # Identity headers change nothing.
        spoofed = client.get('/machine/v1/capabilities', headers={**bearer(w['key']), 'X-NexLoop-Tenant': 'other', 'X-NexLoop-Principal': 'owner'})
        assert spoofed.status_code == 200 and spoofed.json() == {'capabilities': []}
        csrf = owner(client, w)
        assert act(client, csrf, 'deactivate_machine_client', {'client_id': 'acme-agent', 'reason': 'test'}).status_code == 200
        assert client.get('/machine/v1/capabilities', headers=bearer(w['key'])).status_code == 401
    assert counts(w) == {'denied': 3, 'admitted': 1} and metrics(w)['denied_24h'] == 3
    # The allowlisted scope is not enough: no machine grant exists before M5, so the SQL list stays empty.
    assert w['admin'].execute("select count(*) from authz.nexloop_machine_keys where scope=array[%s]", (VERB,)).fetchone()[0] == 1


def test_without_peppers_the_realm_is_unavailable(realm):
    w = realm
    publish(w)
    config = dataclasses.replace(w['config'], machine_key_peppers_file=None, mode='test')
    with TestClient(create_app(config), base_url=ORIGIN) as client:
        r = valid(client.get('/machine/v1/capabilities', headers=bearer(w['key'])))
        assert r.status_code == 503 and r.json()['code'] == 'unavailable'
        assert client.get('/machine/v1/capabilities').status_code == 401  # no credential is still 401


def test_the_metrics_wrapper_keeps_every_grantee(realm):
    db = realm['admin']
    holders = lambda f: {r for (r,) in db.execute("""select r.rolname from pg_proc p cross join aclexplode(p.proacl) a join pg_roles r on r.oid=a.grantee
        where p.oid=%s::regprocedure and a.privilege_type='EXECUTE' and a.grantee<>p.proowner""", (f,)).fetchall()}
    assert holders('runtime.nexloop_metrics_snapshot_before_machine_access(text,text)') == set()
    for role in ('nexloop_api', 'nexloop_runtime', 'nexloop_domain_worker', 'nexloop_action_worker', 'nexloop_scheduler', 'nexloop_configurator'):
        assert not db.execute("select has_function_privilege(%s,'authz.nexloop_machine_capability_list(text,text)','execute')", (role,)).fetchone()[0] == (role != 'nexloop_api')
        for table in ('control.nexloop_machine_rate_policies', 'runtime.nexloop_machine_rate_buckets', 'runtime.nexloop_machine_call_counts'):
            assert not db.execute("select has_table_privilege(%s,%s,'select')", (role, table)).fetchone()[0]
