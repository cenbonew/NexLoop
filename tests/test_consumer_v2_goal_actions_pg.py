"""P0 2026-11-10 repro (read-only investigation, no fix): after a Schema publication moves every real Consumer to v2 (NX-040 F3:
objects upgraded, a successor :2 beside each active Consumer-bound Action), do the workbench goal Actions bound to Consumer still
work at :1 for an existing Consumer? (the effect chain does not: 0179 registrar scope / 0183 'effect Consumer outside Action').

The publication is reproduced as F3 performs it (Consumer v2 type row, successors built by schema_publication.successors(),
objects' schema_version moved to 2); the F3 transaction itself is tests/test_nx040_*. Synthetic data."""
from datetime import timedelta

from psycopg.types.json import Jsonb

from nexloop_eios.object_actions import GovernedObjectCreator
from nexloop_eios.schema_publication import successors
from system_catalog_fixture import insert_active_actions
from support.tenants import TENANT_A
from test_goal_controls import NOW, YEAR, approved_budget, env, goal  # noqa: F401
from test_action_definitions import published_action  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401


def publish_consumer_v2(admin):
    consumer = admin.execute("select definition from ontology.object_type_versions where tenant_id=%s and type_name='Consumer' and version=1",
                             (TENANT_A,)).fetchone()[0]
    v2 = {**consumer, 'version': 2}
    admin.execute("insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,'Consumer',2,%s)", (TENANT_A, Jsonb(v2)))
    rows = admin.execute("""select definition,capability from control.nexloop_action_definitions where tenant_id=%s and world='real' and active
        and definition->'object_types' @> '[{"stable_name":"Consumer"}]'""", (TENANT_A,)).fetchall()
    insert_active_actions(admin, TENANT_A, successors({'actions': [{'definition': d, 'capability': c} for d, c in rows]}, v2))
    admin.execute("update ontology.objects set schema_version=2 where tenant_id=%s and world='real' and type_name='Consumer' and schema_version=1", (TENANT_A,))
    return sorted(d['stable_name'] for d, _ in rows)


def outcome(call):
    try:
        call()
        return 'ok'
    except Exception as failure:  # the repro records what each Action does, it does not judge it
        return f'{type(failure).__name__}: {str(failure).splitlines()[0][:120]}'


def test_goal_actions_at_v1_on_an_existing_consumer_after_consumer_v2(env, admin):  # noqa: F811
    consumer = GovernedObjectCreator(env.reader.pool, env.service, env.reader.signer).create(action_name='Consumer.create', action_version=1,
        intent_id='v2-repro-consumer', type_name='Consumer', properties={})['object_id']
    bound = publish_consumer_v2(admin)
    assert {'Budget.set', 'Control.set', 'Goal.publish', 'Contact.release', 'Metric.approve'} <= set(bound)
    assert admin.execute('select schema_version from ontology.objects where object_id=%s', (consumer,)).fetchone()[0] == 2
    period = (NOW - timedelta(hours=1), NOW + timedelta(days=1))
    approved_budget(admin, 'model', 'CNY', '10', period)  # lowering below an approved budget is Budget.set (NX-056)
    env.reauthenticate()
    owner = env.owner
    results = {
        'Metric.approve': outcome(lambda: owner.approve_metric(action_name='Metric.approve', action_version=1, request_id='v2-metric', metric_id='v2-metric',
            version=1, name='V2 metric', aggregation='count', unit='problem', maturity_seconds=0, refund_rule='not_applicable', cohort_rule='synthetic')),
        'Goal.publish': outcome(lambda: owner.publish_goal(action_name='Goal.publish', action_version=1,
            **goal('v2-goal', 'long_term', 0, YEAR, target='0.5', request_id='v2-goal'))),
        'Control.set': outcome(lambda: owner.set_control(action_name='Control.set', action_version=1, request_id='v2-control', scope_kind='consumer',
            scope_ref='consumer:' + consumer, paused=True, reason='v2 repro')),
        'Budget.set': outcome(lambda: owner.set_budget(action_name='Budget.set', action_version=1, request_id='v2-budget', budget_kind='model', unit='CNY',
            limit_amount='1', period_start=period[0], period_end=period[1])),
        'Contact.release': outcome(lambda: owner.release_contact_restriction(action_name='Contact.release', action_version=1, request_id='v2-release',
            consumer_id=consumer, reason='v2 repro')),
    }
    # Every Action reaches its own handler at :1: no schema scope refusal. Contact.release stops at its business rule (this
    # Consumer has no restriction), which is past the governed entry's checks.
    assert results == {'Metric.approve': 'ok', 'Goal.publish': 'ok', 'Control.set': 'ok', 'Budget.set': 'ok',
                       'Contact.release': 'InvalidParameterValue: no active contact restriction'}, results
