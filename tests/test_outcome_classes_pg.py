"""EIOS gap B5 (NX-061, outcome_classes) and NX-057 R0 (effect_failures): outcome classes, so that a failure rate counts real failures only. A refusal by
rule (contact restriction, the owner's pause, an exhausted budget) is the system working, never a failure; a refused one-reply intent
that 0152 fails is counted once, under its refusal's class; an effect whose result is unknown is neither (reconcile first); a code
without a class counts as a failure until it is classified. Since R0 the effect outcomes are counted by the time of their event:
fulfilled in the hour, first unknown in the hour, a final failure (runtime.nexloop_effect_failures) recorded in the hour - so a
failure decided a day after the intent was made is counted when it is decided. Real PostgreSQL through the NX-030 fixture; admin
seeds synthetic rows as the observability tests do (refusals, effect intents in replica mode, their events and failure rows: the
metric reads rows, it does not drive the effect path; the three paths themselves are tested in their own files)."""
import re
import uuid

import psycopg
import pytest
from datetime import UTC, datetime, timedelta

from psycopg.types.json import Jsonb

from nexloop_eios.run_outcome_taxonomy import (CODE_CLASSES, FAILURE_CLASSES, FAILURE_REASON_CLASSES, RunOutcomeClass, classify, classify_failure,
                                               counts_as_failure)
from test_observability_pg import TENANT, observed  # noqa: F401
from test_workbench_read_pg import browser_business, conversations, identity, published_action, uow, workbench  # noqa: F401


def test_classes_and_which_count_as_failures():
    assert classify('NXC05') is RunOutcomeClass.CONFLICT_PROTECTED and not counts_as_failure(classify('NXC05'))
    assert classify('NXB02') is RunOutcomeClass.TRANSIENT and counts_as_failure(classify('NXB02'))
    assert classify('NXZ99') is RunOutcomeClass.UNRESOLVED and classify(None) is RunOutcomeClass.UNRESOLVED and counts_as_failure(classify('NXZ99'))
    assert FAILURE_CLASSES == {RunOutcomeClass.TRANSIENT, RunOutcomeClass.AUTHORIZATION, RunOutcomeClass.DEFECT, RunOutcomeClass.UNRESOLVED}
    assert not {RunOutcomeClass.SUCCEEDED, RunOutcomeClass.NO_OP, RunOutcomeClass.SUPERSEDED, RunOutcomeClass.CONFLICT_PROTECTED,
                RunOutcomeClass.OUTCOME_UNKNOWN} & FAILURE_CLASSES
    # R0: the reasons of a final effect failure; a refused one-reply intent is counted by its refusal, never twice.
    assert classify_failure('origin_expired') is classify_failure('confirmed_not_delivered') is RunOutcomeClass.DEFECT
    assert classify_failure('refused_one_reply:already_answered') is None and classify_failure('provider_said_no') is RunOutcomeClass.UNRESOLVED


def test_every_refusal_code_has_one_class_in_sql_and_in_python(observed):  # noqa: F811
    admin = observed['admin']
    codes = {c for (c,) in admin.execute('select code from control.nexloop_dispatch_refusal_codes')}
    sql = dict(admin.execute('select code,outcome_class from control.nexloop_outcome_code_classes').fetchall())
    assert codes == set(sql) == set(CODE_CLASSES)  # a new refusal code must be classified in both
    assert sql == {code: outcome.value for code, outcome in CODE_CLASSES.items()}
    # R0: the failure reasons the table accepts are the ones Python classifies (plus the refused one-reply prefix).
    check = admin.execute("""select pg_get_constraintdef(c.oid) from pg_constraint c where c.conrelid='runtime.nexloop_effect_failures'::regclass
        and c.contype='c' and pg_get_constraintdef(c.oid) like '%%reason_code%%'""").fetchone()[0]
    assert set(re.findall(r"'([a-z_]+)'::text", check)) == set(FAILURE_REASON_CLASSES) and 'refused_one_reply:' in check


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


def event(admin, intent_id, kind, at):
    admin.execute("insert into runtime.nexloop_effect_events values(gen_random_uuid(),%s,%s,'real',%s,1,%s,'{}')", (intent_id, TENANT, kind, at))


def failure(admin, intent_id, reason, at):
    admin.execute("""insert into runtime.nexloop_effect_failures(intent_id,tenant_id,world,reason_code,evidence,recorded_at)
        values(%s,%s,'real',%s,'{}',%s)""", (intent_id, TENANT, reason, at))


def test_the_snapshot_counts_by_event_time_and_only_real_failures_in_the_rate(observed):  # noqa: F811
    admin = observed['admin']
    now = datetime.now(UTC)

    def ago(hours):
        return now - timedelta(hours=hours)
    before = outcomes(admin)
    assert before['window_seconds'] == 3600 and before['failure_rate'] == 0
    for _ in range(3):
        refusal(admin, 'NXC05')                                      # contact restricted: protected
    refusal(admin, 'NXB02')                                          # concurrent budget consumption: transient (a failure)
    refusal(admin, 'NXE01')                                          # no provider route: configuration (a failure)
    refused = intent(admin, 'failed')                                # 0152: a refused one-reply intent ends failed ...
    refusal(admin, 'NXC06', intent_id=refused)                       # ... counted once, as the takeover that refused it
    failure(admin, refused, 'refused_one_reply:already_answered', now)
    expired = intent(admin, 'failed', created=ago(2))                # 0174, made two hours ago: a defect now (outcome_classes missed it)
    failure(admin, expired, 'origin_expired', now)
    confirmed = intent(admin, 'failed', created=ago(30))             # 0210: unknown for a day, confirmed not delivered now
    event(admin, confirmed, 'unknown', ago(26))
    failure(admin, confirmed, 'confirmed_not_delivered', now)
    blocked = intent(admin, 'failed', created=ago(3))                # refused by a concurrent budget, then its origin expired:
    refusal(admin, 'NXB02', intent_id=blocked)                       # the refusal and the failure both count (design I-1)
    failure(admin, blocked, 'origin_expired', now)
    pending = intent(admin, 'unknown')                               # sent, not confirmed: reconcile, neither
    event(admin, pending, 'unknown', now)
    late = intent(admin, 'unknown', created=ago(3))                  # its first unknown was counted two hours ago, not again
    event(admin, late, 'unknown', ago(2)), event(admin, late, 'unknown', now)
    quick = intent(admin, 'fulfilled')                               # unknown, then fulfilled within the hour: succeeded only (L6 M-1)
    event(admin, quick, 'unknown', ago(0.5)), event(admin, quick, 'fulfilled', now)
    for created in (ago(2), now):                                    # fulfilled in the hour, whenever made: succeeded
        event(admin, intent(admin, 'fulfilled', created=created), 'fulfilled', now)
    event(admin, intent(admin, 'fulfilled', created=ago(3)), 'fulfilled', ago(2))  # fulfilled outside the hour
    intent(admin, 'accepted')                                        # in flight: not decided yet
    failure(admin, intent(admin, 'failed', created=ago(3)), 'origin_expired', ago(2))  # failed outside the hour
    admin.execute("select set_config('eios.tenant_id','',false)")  # as the metrics export calls it: no tenant set (FORCE RLS tables)
    out = outcomes(admin)
    assert admin.execute("select current_setting('eios.tenant_id',true)").fetchone() == ('',)  # restored
    classes = {k: int(v) for k, v in out['classes'].items()}
    assert classes == {'conflict_protected': 4, 'transient': 2, 'authorization': 1, 'defect': 3, 'outcome_unknown': 1, 'succeeded': 3}
    assert int(out['decided']) == 14 and int(out['failures']) == 6
    assert float(out['failure_rate']) == round(6 / 14, 4) and float(out['failure_rate_counting_refusals']) == round(10 / 14, 4)
    # A new code nobody classified counts as a failure (fail closed) and is shown as unresolved.
    admin.execute("insert into control.nexloop_dispatch_refusal_codes values ('NXZ99','synthetic_unclassified')")
    refusal(admin, 'NXZ99')
    out = outcomes(admin)
    assert int(out['unresolved']) == 1 and int(out['failures']) == 7 and float(out['failure_rate']) == round(7 / 15, 4)


def test_an_intent_failed_without_its_failure_row_is_refused_at_commit(observed):  # noqa: F811
    """R0: the three paths write the failure row in the transaction that fails the intent; a fourth path that forgets it is refused
    at commit, so no final failure can escape the evidence that remediation (NX-057) and the failure rate read."""
    admin = observed['admin']
    lost = intent(admin, 'accepted')
    with pytest.raises(psycopg.errors.CheckViolation, match='effect failure evidence missing'), admin.transaction():
        admin.execute("update runtime.nexloop_effect_intents set state='failed' where intent_id=%s", (lost,))
    assert admin.execute('select state from runtime.nexloop_effect_intents where intent_id=%s', (lost,)).fetchone() == ('accepted',)
    with admin.transaction():                                        # with its row, in the same transaction: committed
        admin.execute("update runtime.nexloop_effect_intents set state='failed' where intent_id=%s", (lost,))
        failure(admin, lost, 'origin_expired', datetime.now(UTC))
    with pytest.raises(psycopg.errors.UniqueViolation), admin.transaction():  # one failure per intent
        failure(admin, lost, 'confirmed_not_delivered', datetime.now(UTC))
    with pytest.raises(psycopg.Error), admin.transaction():                   # append-only
        admin.execute("update runtime.nexloop_effect_failures set reason_code='confirmed_not_delivered' where intent_id=%s", (lost,))
    with pytest.raises(psycopg.errors.CheckViolation), admin.transaction():   # a reason outside the vocabulary
        failure(admin, intent(admin, 'accepted'), 'provider_said_no', datetime.now(UTC))
    for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_action_worker', 'nexloop_scheduler', 'nexloop_runtime', 'nexloop_metrics'):
        assert admin.execute("select has_table_privilege(%s,'runtime.nexloop_effect_failures','select,insert')", (role,)).fetchone() == (False,)
    assert admin.execute("select relforcerowsecurity from pg_class where oid='runtime.nexloop_effect_failures'::regclass").fetchone() == (True,)
