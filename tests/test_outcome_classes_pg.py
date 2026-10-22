"""EIOS gap B5 (NX-061, 0324): outcome classes, so that a failure rate counts real failures only. A refusal by rule (contact
restriction, the owner's pause, an exhausted budget) is the system working, never a failure; a refused one-reply intent that 0152
fails is counted once, under its refusal's class; an effect whose result is unknown is neither (reconcile first); a code without a
class counts as a failure until it is classified. Real PostgreSQL through the NX-030 fixture; admin seeds synthetic events as the
observability tests do (refusals, effect intents in replica mode: the metric reads rows, it does not drive the effect path)."""
import uuid
from datetime import UTC, datetime, timedelta

from psycopg.types.json import Jsonb

from nexloop_eios.run_outcome_taxonomy import CODE_CLASSES, FAILURE_CLASSES, RunOutcomeClass, classify, counts_as_failure
from test_observability_pg import TENANT, observed  # noqa: F401
from test_workbench_read_pg import browser_business, conversations, identity, published_action, uow, workbench  # noqa: F401


def test_classes_and_which_count_as_failures():
    assert classify('NXC05') is RunOutcomeClass.CONFLICT_PROTECTED and not counts_as_failure(classify('NXC05'))
    assert classify('NXB02') is RunOutcomeClass.TRANSIENT and counts_as_failure(classify('NXB02'))
    assert classify('NXZ99') is RunOutcomeClass.UNRESOLVED and classify(None) is RunOutcomeClass.UNRESOLVED and counts_as_failure(classify('NXZ99'))
    assert FAILURE_CLASSES == {RunOutcomeClass.TRANSIENT, RunOutcomeClass.AUTHORIZATION, RunOutcomeClass.DEFECT, RunOutcomeClass.UNRESOLVED}
    assert not {RunOutcomeClass.SUCCEEDED, RunOutcomeClass.NO_OP, RunOutcomeClass.SUPERSEDED, RunOutcomeClass.CONFLICT_PROTECTED,
                RunOutcomeClass.OUTCOME_UNKNOWN} & FAILURE_CLASSES


def test_every_refusal_code_has_one_class_in_sql_and_in_python(observed):  # noqa: F811
    admin = observed['admin']
    codes = {c for (c,) in admin.execute('select code from control.nexloop_dispatch_refusal_codes')}
    sql = dict(admin.execute('select code,outcome_class from control.nexloop_outcome_code_classes').fetchall())
    assert codes == set(sql) == set(CODE_CLASSES)  # a new refusal code must be classified in both
    assert sql == {code: outcome.value for code, outcome in CODE_CLASSES.items()}


def intent(admin, state, *, created=None):
    intent_id = uuid.uuid4()
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')  # synthetic row: the context and run it would reference are not seeded
        admin.execute("""insert into runtime.nexloop_effect_intents(intent_id,receipt_id,context_id,tenant_id,world,consumer_id,goal_identity,
            plan_step_identity,slot_identity,action_name,action_version,business_digest,provider_payload_digest,frozen_request,action_definition,
            capability,executor_principal,control_revision,created_at,state,governed_claim_finalized) values(%s,%s,%s,%s,'real','consumer-synthetic',%s,%s,%s,
            'synthetic.send',1,%s,%s,%s,%s,%s,'executor-synthetic',1,%s,%s,%s)""",
            (intent_id, uuid.uuid4(), uuid.uuid4(), TENANT, 'goal-' + intent_id.hex[:8], 'step-' + intent_id.hex[:8], 'slot-' + intent_id.hex[:8],
             'a' * 64, 'b' * 64, Jsonb({}), Jsonb({}), Jsonb({}), created or datetime.now(UTC), state, state in ('fulfilled', 'confirmed')))
    return intent_id


def refusal(admin, code, *, intent_id=None):
    if intent_id is None:
        admin.execute("insert into runtime.nexloop_dispatch_refusals(tenant_id,world,path,code,task_id) values(%s,'real','run',%s,%s)",
                      (TENANT, code, 'task-' + uuid.uuid4().hex[:12]))
    else:
        admin.execute("insert into runtime.nexloop_dispatch_refusals(tenant_id,world,path,code,intent_id) values(%s,'real','effect',%s,%s)",
                      (TENANT, code, intent_id))


def outcomes(admin):
    return admin.execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'outcomes'", (TENANT,)).fetchone()[0]


def test_the_snapshot_counts_refusals_by_class_and_only_real_failures_in_the_rate(observed):  # noqa: F811
    admin = observed['admin']
    before = outcomes(admin)
    assert before['window_seconds'] == 3600 and before['failure_rate'] == 0
    for _ in range(3):
        refusal(admin, 'NXC05')                                      # contact restricted: protected
    refusal(admin, 'NXB02')                                          # concurrent budget consumption: transient (a failure)
    refusal(admin, 'NXE01')                                          # no provider route: configuration (a failure)
    refused = intent(admin, 'failed')                                # 0152: a refused one-reply intent ends failed ...
    refusal(admin, 'NXC06', intent_id=refused)                       # ... counted once, as the takeover that refused it
    intent(admin, 'failed')                                          # failed with no refusal: a defect
    intent(admin, 'unknown')                                         # sent, not confirmed: reconcile, neither
    intent(admin, 'fulfilled'), intent(admin, 'confirmed')           # succeeded
    intent(admin, 'accepted')                                        # in flight: not decided yet
    intent(admin, 'failed', created=datetime.now(UTC) - timedelta(hours=2))  # outside the hour
    out = outcomes(admin)
    classes = {k: int(v) for k, v in out['classes'].items()}
    assert classes == {'conflict_protected': 4, 'transient': 1, 'authorization': 1, 'defect': 1, 'outcome_unknown': 1, 'succeeded': 2}
    assert int(out['decided']) == 10 and int(out['failures']) == 3
    assert float(out['failure_rate']) == 0.3 and float(out['failure_rate_counting_refusals']) == 0.7  # what a naive rate would show
    # A new code nobody classified counts as a failure (fail closed) and is shown as unresolved.
    admin.execute("insert into control.nexloop_dispatch_refusal_codes values ('NXZ99','synthetic_unclassified')")
    refusal(admin, 'NXZ99')
    out = outcomes(admin)
    assert int(out['unresolved']) == 1 and int(out['failures']) == 4 and float(out['failure_rate']) == round(4 / 11, 4)
