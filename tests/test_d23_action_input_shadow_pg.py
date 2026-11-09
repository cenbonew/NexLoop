"""D23 option B, batch 0 (migration action_input_shadow; design §3 C3): candidate input schemas validated at dispatch, counted, never
enforced. On the governed Consumer.edit path of tests/test_claim_matching_pg.py: a candidate that refuses everything leaves the
write applied and counts one call and one would-reject; an accepting one counts a call only; counts carry the Action version and
the candidate digest - no request body, no principal; the report reads pass / fail / no_data / stale for the read-only metrics login;
candidates come from trusted configuration only, for active Action versions only; another tenant's caller counts nothing here."""
import json
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from test_claim_matching_pg import decision, env, matcher, obj  # noqa: F401

RESOURCE = 'eios:action:Consumer.edit:1'
REFUSE_ALL = {'not': {}}
ACCEPT_ALL = {'type': 'object'}


def configure(f, candidates):
    f['admin'].execute('alter role nexloop_configurator login')
    with psycopg.connect(make_conninfo(f['pg'], user='nexloop_configurator'), autocommit=True) as c:
        return c.execute('select control.nexloop_configure_input_candidates(%s,%s,%s)', (f['tenant'], 'real', Jsonb(candidates))).fetchone()[0]


def report(f, since=None):
    with psycopg.connect(make_conninfo(f['pg'], user='nexloop_metrics'), autocommit=True) as c:
        rows = c.execute('select authz.nexloop_action_input_shadow_report(%s,%s,%s)',
                         (f['tenant'], 'real', since or datetime.now(UTC) - timedelta(days=7))).fetchone()[0]
    return {r['resource_id']: r for r in rows}


def shadow_rows(f):
    return f['admin'].execute('select resource_id,calls,would_reject from runtime.nexloop_action_input_shadow where tenant_id=%s', (f['tenant'],)).fetchall()


def test_a_refusing_candidate_is_counted_and_never_blocks_the_governed_write(env):
    f = env
    assert configure(f, [{'resource_id': RESOURCE, 'input_schema': REFUSE_ALL}]) == {'revision': 1, 'candidates': 1}
    assert report(f)[RESOURCE]['status'] == 'no_data'  # configured, not called yet: never publishable (C3)
    claim = f['claims'].add('shadow-budget', '预算区间', '两千元以内', quote='预算在两千元以内')
    m, _ = matcher(f, {claim: decision('eios:property:Consumer/budget_level', '两千元以内')}, suffix='-shadow')
    assert list(m.process_conversation(f['conversation'])['applied'].values()) == ['applied']  # the shadow never enforces
    assert obj(f['admin'], f['consumer'])[0]['budget_level'] == '两千元以内'
    assert shadow_rows(f) == [(RESOURCE, 1, 1)]
    r = report(f)[RESOURCE]
    assert (r['calls'], r['would_reject'], r['status']) == (1, 1, 'fail')
    # Only the Action version, the candidate digest, the day and two counts: nothing of the request or the caller.
    stored = json.dumps(f['admin'].execute('select to_jsonb(s) from runtime.nexloop_action_input_shadow s').fetchall(), default=str)
    assert '两千元以内' not in stored and 'budget_level' not in stored and f['consumer'] not in stored
    columns = {r[0] for r in f['admin'].execute("""select column_name from information_schema.columns
        where table_schema='runtime' and table_name='nexloop_action_input_shadow'""").fetchall()}
    assert columns == {'tenant_id', 'world', 'resource_id', 'candidate_digest', 'day', 'calls', 'would_reject'}


def test_an_accepting_candidate_passes_and_a_new_candidate_starts_counting_again(env):
    f = env
    configure(f, [{'resource_id': RESOURCE, 'input_schema': ACCEPT_ALL}])
    claim = f['claims'].add('shadow-accept', '预算区间', '两千元以内', quote='预算在两千元以内')
    m, _ = matcher(f, {claim: decision('eios:property:Consumer/budget_level', '两千元以内')}, suffix='-shadow-accept')
    m.process_conversation(f['conversation'])
    r = report(f)[RESOURCE]
    assert (r['calls'], r['would_reject'], r['status']) == (1, 0, 'pass')
    configure(f, [{'resource_id': RESOURCE, 'input_schema': REFUSE_ALL}])  # another candidate: its own digest, its own counts
    assert report(f)[RESOURCE]['status'] == 'no_data'


def test_candidates_only_through_trusted_configuration_and_only_for_active_versions(env):
    f = env
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        f['admin'].execute('select control.nexloop_configure_input_candidates(%s,%s,%s)', (f['tenant'], 'real', Jsonb([])))
    for bad in ([{'resource_id': 'eios:action:Consumer.edit:9', 'input_schema': ACCEPT_ALL}],
                [{'resource_id': RESOURCE, 'input_schema': []}], [{'resource_id': RESOURCE, 'input_schema': ACCEPT_ALL, 'note': 'x'}]):
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            configure(f, bad)
    configure(f, [{'resource_id': RESOURCE, 'input_schema': ACCEPT_ALL}])
    f['admin'].execute("update control.nexloop_action_definitions set active=false where tenant_id=%s and resource_id=%s", (f['tenant'], RESOURCE))
    assert report(f)[RESOURCE]['status'] == 'stale'


def test_the_report_is_the_metrics_login_s_and_a_caller_without_a_tenant_counts_nothing(env):
    f = env
    configure(f, [{'resource_id': RESOURCE, 'input_schema': ACCEPT_ALL}])
    digest = f['admin'].execute('select candidate_digest from control.nexloop_action_input_candidates where tenant_id=%s', (f['tenant'],)).fetchone()[0]
    with psycopg.connect(make_conninfo(f['pg'], user='nexloop_api'), autocommit=True) as api:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            api.execute('select authz.nexloop_action_input_shadow_report(%s,%s,now())', (f['tenant'], 'real'))
        assert api.execute('select authz.nexloop_action_input_shadow_record(%s,%s,%s,%s,true)', ('0' * 64, 'real', RESOURCE, digest)).fetchone()[0] is False
        assert api.execute('select authz.nexloop_action_input_candidate(%s,%s,%s)', ('0' * 64, 'real', RESOURCE)).fetchone()[0] is None
    assert shadow_rows(f) == []


class _Rollback(Exception):
    pass


def test_only_an_identity_refusal_means_no_tenant_any_other_error_is_raised(env):
    """L6 G1 on 0431: authz.nexloop_action_input_tenant turns only 42501 (the identity refusal) into "no tenant"; a database fault or
    a bug (here an injected 22P02 inside the identity resolution, in a transaction that is rolled back) is raised, never hidden as
    a missing tenant."""
    f = env
    assert f['admin'].execute('select authz.nexloop_action_input_tenant(%s,%s)', ('0' * 64, 'real')).fetchone()[0] is None  # 42501
    with pytest.raises(_Rollback):
        with f['admin'].transaction():
            # Same function (same oid, so the caller's cached plan reaches it), another body; the rollback restores it.
            f['admin'].execute("""create or replace function authz.nexloop_service_identity(p_digest text,p_world text) returns jsonb language plpgsql
                as $$ begin return jsonb_build_object('binding', jsonb_build_object('tenant_id', 'not-a-number'::int)); end $$""")
            with pytest.raises(psycopg.errors.InvalidTextRepresentation):
                with f['admin'].transaction():
                    f['admin'].execute('select authz.nexloop_action_input_tenant(%s,%s)', ('0' * 64, 'real'))
            raise _Rollback
    assert 'not-a-number' not in f['admin'].execute("select pg_get_functiondef('authz.nexloop_service_identity(text,text)'::regprocedure)").fetchone()[0]


def test_a_shadow_that_fails_leaves_dispatch_untouched_counts_nothing_and_counts_the_failure(env):
    """The governor side: a shadow error other than an identity refusal (an injected 22P02 in the candidate lookup) never blocks the
    governed write, records no shadow row, and is counted in action_governor.SHADOW_FAILURES (with a warning)."""
    from nexloop_eios import action_governor as G
    f = env
    configure(f, [{'resource_id': RESOURCE, 'input_schema': REFUSE_ALL}])
    original = f['admin'].execute("select pg_get_functiondef('authz.nexloop_action_input_candidate(text,text,text)'::regprocedure)").fetchone()[0]
    f['admin'].execute("""create or replace function authz.nexloop_action_input_candidate(p_digest text,p_world text,p_resource text) returns jsonb
        language plpgsql stable security definer set search_path=pg_catalog,pg_temp as $$ begin return jsonb_build_object('n', 'x'::int); end $$""")
    try:
        before = G.SHADOW_FAILURES['count']
        claim = f['claims'].add('shadow-fault', '预算区间', '两千元以内', quote='预算在两千元以内')
        m, _ = matcher(f, {claim: decision('eios:property:Consumer/budget_level', '两千元以内')}, suffix='-shadow-fault')
        assert list(m.process_conversation(f['conversation'])['applied'].values()) == ['applied']
        assert obj(f['admin'], f['consumer'])[0]['budget_level'] == '两千元以内'
        assert shadow_rows(f) == []
        assert G.SHADOW_FAILURES['count'] == before + 1
    finally:
        f['admin'].execute(original)
