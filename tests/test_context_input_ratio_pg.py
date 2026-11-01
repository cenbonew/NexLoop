"""S86 step 1 (migration context_input_ratio, alert rules v28) on real PostgreSQL: the metrics snapshot's context_input_ratio block,
provider input / the strategy's input_token_budget over 24 hours per model and Run kind. Without a measured request it says
present: false and carries no ratio (never a 0 that reads as healthy); configured says whether a strategy is published. The v28
rules read the block through the evaluator's own path extraction (0131: one '*' segment, numbers only, ge threshold), the S78
positive control: the stored rules against the live snapshot, and against it with one ratio raised. Synthetic rows only."""
import json
import uuid
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.observability import load_rules
from support.tenants import TENANT_A as TENANT

RULES = Path(__file__).resolve().parents[1] / 'deploy/configuration'
NEW = ('context_input_ratio_p95', 'context_input_ratio_max')
# The evaluator's extraction (0131): metric 'a.b.*.c' iterates the children of a.b, the child key is the selector, numbers only.
FIRED = """
with rule as (select x from jsonb_array_elements(%s::jsonb) x),
     snap as (select %s::jsonb v)
select x->>'rule_id', e.key
  from rule, snap,
       jsonb_each(case when jsonb_typeof(v#>string_to_array(split_part(x->>'metric','.*.',1),'.'))='object'
                       then v#>string_to_array(split_part(x->>'metric','.*.',1),'.') else '{}'::jsonb end) e
 where jsonb_typeof(e.value#>string_to_array(split_part(x->>'metric','.*.',2),'.'))='number'
   and (e.value#>>string_to_array(split_part(x->>'metric','.*.',2),'.'))::numeric >= (x->>'threshold')::numeric
 order by 1, 2"""


@pytest.fixture
def db(admin):
    bootstrap(admin)
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (TENANT,))
    return admin


def block(admin, world='real'):
    return admin.execute("select runtime.nexloop_metrics_snapshot(%s,%s)", (TENANT, world)).fetchone()[0].get('context_input_ratio')


def v28_rules():
    return [r for r in load_rules(RULES / 'alert-rules.v28.json')['rules'] if r['rule_id'] in NEW]


def fired(admin, snapshot):
    return [tuple(r) for r in admin.execute(FIRED, (json.dumps(v28_rules()), json.dumps(snapshot))).fetchall()]


def strategy(admin):
    d = {'strategy_id': 'recent_plus_required', 'version': 1}
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("insert into control.nexloop_context_strategies(tenant_id,world,strategy_id,version,definition,definition_digest,published_by,intent_id) "
                      "values(%s,'real','recent_plus_required',1,%s,%s,'synthetic-human-owner','seed-strategy')", (TENANT, Jsonb(d), 'a' * 64))


def request(admin, *, provider_input, budget=16000, model='deepseek-flash', kind=None, cache_read=0, age="0 seconds", usage=None):
    run, context = uuid.uuid4(), uuid.uuid4()
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')  # no Run credential, route or artifact rows behind the synthetic ones
        admin.execute("insert into runtime.nexloop_context_packs(tenant_id,world,context_id,run_id,protocol,strategy_ref,pack_digest,artifact_id,budget_report,insufficient) "
                      "values(%s,'real',%s,%s,'nexloop.context-pack.v6','context-strategy:recent_plus_required@1',%s,%s,%s,'[]')",
                      (TENANT, context, run, 'b' * 64, 'c' * 32, Jsonb({'input_token_budget': budget, 'output_reserve': 4000, 'framing_reserve': 3900, 'used': 1})))
        admin.execute("insert into runtime.nexloop_model_requests(tenant_id,world,run_id,call_sequence,context_id,model_provider,model_id,tool_manifest_digest,"
                      "request_digest,prompt_artifact_id,input_token_budget,output_token_budget,settings_digest,requested_at,result_status,usage,completed_at) "
                      f"values(%s,'real',%s,1,%s,'deepseek',%s,%s,%s,%s,%s,4000,%s,clock_timestamp()-interval '{age}','succeeded',%s,clock_timestamp())",
                      (TENANT, run, context, model, 'd' * 64, 'e' * 64, 'f' * 32, budget, '0' * 64,
                       Jsonb(usage if usage is not None else {'input': provider_input, 'output': 10, 'cache_read': cache_read, 'cache_write': 0})))
        if kind == 'plan':
            admin.execute("insert into runtime.nexloop_plan_runs(tenant_id,world,run_id,plan_id,version,triggers) values(%s,'real',%s,%s,1,'[]')",
                          (TENANT, run, uuid.uuid4()))
    return run


def test_v28_adds_exactly_the_two_ratio_rules_on_v27():
    v27, v28 = json.loads((RULES / 'alert-rules.v27.json').read_text()), load_rules(RULES / 'alert-rules.v28.json')
    assert v28['version'] == 28 and len(v27['rules']) == 93 and len(v28['rules']) == 95 and v28['rules'][:93] == v27['rules']
    assert v28['decision'].startswith(v27['decision'] + ' v28 ')
    p95, mx = v28['rules'][93:]
    assert (p95['rule_id'], p95['metric'], p95['condition'], p95['threshold'], p95['severity']) == (
        'context_input_ratio_p95', 'context_input_ratio.groups.*.p95', 'ge', 0.9, 'warning')
    assert (mx['rule_id'], mx['metric'], mx['condition'], mx['threshold'], mx['severity']) == (
        'context_input_ratio_max', 'context_input_ratio.groups.*.max', 'ge', 1.0, 'critical')


def test_without_a_measured_request_the_block_says_so_and_carries_no_ratio(db):
    assert block(db) == {'configured': False, 'present': False, 'requests_24h': 0}
    strategy(db)
    assert block(db) == {'configured': True, 'present': False, 'requests_24h': 0}
    # Not measured: older than 24 hours, a usage without a numeric input, or another world.
    request(db, provider_input=20000, age='25 hours')
    request(db, provider_input=None, usage={'output': 10})
    assert block(db) == {'configured': True, 'present': False, 'requests_24h': 0}
    assert 'context_input_ratio' not in db.execute("select runtime.nexloop_metrics_snapshot(%s,'test')", (TENANT,)).fetchone()[0]
    snapshot = db.execute("select runtime.nexloop_metrics_snapshot(%s,'real')", (TENANT,)).fetchone()[0]
    assert fired(db, snapshot) == []  # nothing to compare: the rules see no number (the block's present: false says why)


def test_ratios_per_model_and_run_kind_and_the_rules_fire_on_them(db):
    strategy(db)
    for value in (8000, 9000, 10000):
        request(db, provider_input=value)  # message kind not derivable here: 'other'
    request(db, provider_input=14000, cache_read=1000, kind='plan')  # 15000 / 16000
    b = block(db)
    assert (b['configured'], b['present'], b['requests_24h']) == (True, True, 4)
    assert b['groups'] == {'deepseek-flash|other': {'requests': 3, 'p95': 0.6188, 'max': 0.625},
                           'deepseek-flash|plan': {'requests': 1, 'p95': 0.9375, 'max': 0.9375}}
    assert b['max'] == 0.9375 and 0.62 < b['p95'] <= 0.9375
    snapshot = db.execute("select runtime.nexloop_metrics_snapshot(%s,'real')", (TENANT,)).fetchone()[0]
    # The stored rule paths resolve to these numbers: the plan group is at 0.9375 (p95 fires), no request reached the budget.
    assert fired(db, snapshot) == [('context_input_ratio_p95', 'deepseek-flash|plan')]
    # Positive control without data changes: the same snapshot with the other group's max raised to 1.0 fires the critical rule.
    raised = json.loads(json.dumps(snapshot)); raised['context_input_ratio']['groups']['deepseek-flash|other']['max'] = 1.0
    assert ('context_input_ratio_max', 'deepseek-flash|other') in fired(db, raised)
    request(db, provider_input=16500, model='deepseek-pro')
    snapshot = db.execute("select runtime.nexloop_metrics_snapshot(%s,'real')", (TENANT,)).fetchone()[0]
    assert set(fired(db, snapshot)) == {('context_input_ratio_p95', 'deepseek-flash|plan'), ('context_input_ratio_p95', 'deepseek-pro|other'),
                                        ('context_input_ratio_max', 'deepseek-pro|other')}


def test_the_block_wraps_the_previous_snapshot(db):
    keys = set(db.execute("select runtime.nexloop_metrics_snapshot(%s,'real')", (TENANT,)).fetchone()[0])
    assert {'context_input_ratio', 'commitment_orphans', 'workbench_read_types', 'action_claims'} <= keys
    assert db.execute("select count(*) from pg_proc where proname='nexloop_metrics_snapshot_before_context_input_ratio'").fetchone()[0] == 1
