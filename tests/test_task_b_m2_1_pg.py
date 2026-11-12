"""Option B M2.1 (ADR-046 §3; design draft §2.2-§2.4): with the effect Action successor :2 admitted for Consumer v2, the whole chain
runs at :2 - the relay issues the message Run with :2 (allowed_actions, source proof, run credential), the bind proves :2 and stores
it in the context, the runtime submits the intent at :2 and the effect worker delivers it at :2 - and no A' proof is written. With
only :1 admitted nothing changes (test_effect_action_admissions_pg.test_a_prime_stays_through_m1 and the A' tests are the control).
Synthetic data, actual PostgreSQL."""
import json

import psycopg

import pytest

from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401
from test_context_artifacts import active_worker, context_message  # noqa: F401
from test_effect_scope_successor_proof_pg import proofs, reauthenticate, reply
from test_local_json_delivery_pg import delivery_plan, live_delivery  # noqa: F401
from nexloop_eios.effect_dispatch import EffectDispatcher
from task_b_fixture import EFFECT, admit, bump_consumer_v2


def chain(admin):
    return {
        'contexts': sorted(r[0] for r in admin.execute('select action_resource from control.nexloop_effect_contexts').fetchall()),
        'runs': sorted(json.dumps(r[0]) for r in admin.execute("""select allowed_resources from authz.nexloop_run_credentials
            where source_digest is not null and exists(select 1 from unnest(allowed_resources) x where x like %s)""", (f'eios:action:{EFFECT}:%',)).fetchall()),
        'intents': sorted((r[0], r[1]) for r in admin.execute('select action_name,(action_definition->>%s)::integer from runtime.nexloop_effect_intents', ('version',)).fetchall()),
    }


def test_the_admitted_successor_carries_the_relay_reply_at_2(context_message, admin, tmp_path):  # noqa: F811
    f = context_message
    bump_consumer_v2(admin, f['original']['tenant'])
    reauthenticate(f)
    receipt = reply(f, tmp_path)
    assert isinstance(receipt, dict) and receipt['intent_id'], receipt
    found = chain(admin)
    assert found['contexts'] == [f'eios:action:{EFFECT}:2']
    assert found['runs'] == [json.dumps([f'eios:action:{EFFECT}:2'])]
    assert found['intents'] == [(EFFECT, 2)]
    assert proofs(admin) == []  # exact: no A' proof


def test_without_the_admission_the_reply_stays_at_1_through_a_prime(context_message, admin, tmp_path):  # noqa: F811
    f = context_message
    publication = bump_consumer_v2(admin, f['original']['tenant'], admitted=False)
    reauthenticate(f)
    receipt = reply(f, tmp_path)
    assert isinstance(receipt, dict), receipt
    found = chain(admin)
    assert found['contexts'] == [f'eios:action:{EFFECT}:1'] and found['intents'] == [(EFFECT, 1)]
    assert proofs(admin) == [(f'eios:action:{EFFECT}:1', f'eios:action:{EFFECT}:2', publication, 2)]


def test_a_bind_naming_another_version_than_the_selection_is_refused(context_message, admin, tmp_path, monkeypatch):  # noqa: F811
    """M2-a: the registrar command recomputes the selection; proofs naming :1 for an admitted :2 are refused, nothing is bound."""
    from nexloop_eios.effect_contexts import EffectContextRegistrar
    from nexloop_eios.message_relay import MessageRelayPort, MessageRelayUnavailable
    f = context_message
    bump_consumer_v2(admin, f['original']['tenant'])
    reauthenticate(f)
    monkeypatch.setattr(EffectContextRegistrar, '_effect_version', lambda self, db, verb, payload: 1)
    original = MessageRelayPort.effect_resources
    monkeypatch.setattr(MessageRelayPort, 'effect_resources', lambda self, source, consumer: {k: v.rsplit(':', 1)[0] + ':1' for k, v in original(self, source, consumer).items()})
    with pytest.raises(MessageRelayUnavailable):
        f['relay'].run_once()
    assert admin.execute('select count(*) from control.nexloop_effect_contexts').fetchone()[0] == 0


def test_a_run_naming_another_version_than_the_selection_is_not_issued(context_message, admin, tmp_path, monkeypatch):  # noqa: F811
    """The issuance command recomputes the selection for the Consumer: allowed_actions naming :1 for an admitted :2 issue no Run."""
    from nexloop_eios.message_relay import MessageRelayPort, MessageRelayUnavailable
    f = context_message
    bump_consumer_v2(admin, f['original']['tenant'])
    reauthenticate(f)
    original = MessageRelayPort.effect_resources
    monkeypatch.setattr(MessageRelayPort, 'effect_resources', lambda self, source, consumer: {k: v.rsplit(':', 1)[0] + ':1' for k, v in original(self, source, consumer).items()})
    with pytest.raises(MessageRelayUnavailable):
        f['relay'].run_once()
    assert chain(admin)['runs'] == []


def test_retiring_a_version_an_unexpired_context_names_is_refused(context_message, admin, tmp_path):  # noqa: F811
    """M2-b: refused while a context names it (contexts are immutable and signed for their resource); nothing changes."""
    f = context_message
    tenant = f['original']['tenant']
    bump_consumer_v2(admin, tenant)
    reauthenticate(f)
    assert isinstance(reply(f, tmp_path), dict)
    admin.execute('set session authorization nexloop_configurator')
    try:
        out = admin.execute("select control.nexloop_retire_effect_action_admission(%s,%s,2,'human:owner','合成退役')", (tenant, EFFECT)).fetchone()[0]
    finally:
        admin.execute('reset session authorization')
    assert out == {'retired': False, 'admission_id': None, 'code': 'referenced_by_unexpired_context'}
    assert admin.execute('select count(*) from control.nexloop_effect_action_admissions where retired_at is null').fetchone()[0] == 1
    admin.execute("update control.nexloop_effect_contexts set valid_until=clock_timestamp()-interval '1 second'")
    admin.execute('set session authorization nexloop_configurator')
    try:
        out = admin.execute("select control.nexloop_retire_effect_action_admission(%s,%s,2,'human:owner','合成退役')", (tenant, EFFECT)).fetchone()[0]
    finally:
        admin.execute('reset session authorization')
    assert out['retired'] is True


@pytest.mark.parametrize('delivery_plan', ['consumer-v2'], indirect=True)
def test_the_effect_worker_delivers_the_admitted_successor_at_2(delivery_plan, admin):  # noqa: F811
    f = delivery_plan
    assert f['effect_version'] == 2
    accepted = f['submitter'].submit_effect_intent(parameters={'message': 'v2 合成本地投递'}, action_version=2)
    intent = accepted['intent_id']
    with live_delivery(f):
        result = EffectDispatcher(f['executor'], f['provider'], lease_seconds=30).run_once()
        assert result['status'] == 'fulfilled' and result['business_action_success'] is True and result['governed_claim_finalized'] is True
    assert admin.execute("select action_definition->>'version' from runtime.nexloop_effect_intents where intent_id=%s", (intent,)).fetchone() == ('2',)
    assert admin.execute('select action_resource from control.nexloop_effect_contexts').fetchall() == [(f'eios:action:{EFFECT}:2',)]


@pytest.mark.parametrize('delivery_plan', ['consumer-v2-undeclared'], indirect=True)
def test_an_undeclared_successor_category_keeps_the_dispatch_closed(delivery_plan, admin):  # noqa: F811
    """The effect category (and externality) is declared per definition digest; :2 without a declaration is external and
    customer_contact (fail closed): the worker does not deliver it until trusted configuration declares :2 (run sheet, M3)."""
    f = delivery_plan
    accepted = f['submitter'].submit_effect_intent(parameters={'message': 'v2 未声明分类'}, action_version=2)
    with live_delivery(f):
        result = EffectDispatcher(f['executor'], f['provider'], lease_seconds=30).run_once()
    assert result['status'] == 'admission_unavailable' and result['business_action_success'] is False
    assert not list(f['root'].glob(accepted['intent_id'] + '*'))


@pytest.mark.parametrize('delivery_plan', ['consumer-v2'], indirect=True)
def test_configure_proving_another_version_than_the_selection_is_refused(delivery_plan, monkeypatch):  # noqa: F811
    """M2-a for configure (executor proof only, so no Run check in front of it): the executor holds :1 and :2; proving :1 for a
    control whose Consumer selects :2 is refused by the recomputation, proving :2 is accepted."""
    from nexloop_eios.effect_contexts import EffectContextRegistrar
    f = delivery_plan
    owner = f['api'].authenticate(f['owner_token'], world='real')
    executor_token = f['credential_file'].read_text()
    with monkeypatch.context() as m:
        m.setattr(EffectContextRegistrar, '_effect_version', lambda self, db, verb, payload: 1)
        with pytest.raises(Exception, match='unavailable'):
            owner.configure_effect_control(control_id=f['control'], control_revision=1, executor_token=executor_token)
    assert owner.configure_effect_control(control_id=f['control'], control_revision=1, executor_token=executor_token)['control_id'] == f['control']


@pytest.mark.parametrize('delivery_plan', ['consumer-v2-wide-run'], indirect=True)
def test_an_intent_naming_another_version_than_its_context_is_refused(delivery_plan, admin):  # noqa: F811
    """The Run may hold :1 and :2; the context is :2, so an intent at :1 is refused (it is not the context's resource), at :2 accepted."""
    f = delivery_plan
    with pytest.raises(Exception, match='unavailable'):
        f['submitter'].submit_effect_intent(parameters={'message': 'v1 越界'}, action_version=1)
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone() == (0,)
    accepted = f['submitter'].submit_effect_intent(parameters={'message': 'v2 正常'}, action_version=2)
    assert admin.execute("select action_definition->>'version' from runtime.nexloop_effect_intents where intent_id=%s", (accepted['intent_id'],)).fetchone() == ('2',)


@pytest.mark.parametrize('delivery_plan', ['consumer-v2-wide-unbound'], indirect=True)
def test_a_bind_proving_another_version_than_the_selection_is_refused_by_the_recomputation(delivery_plan, admin, monkeypatch):  # noqa: F811
    """M2-a for bind, isolated: the Run holds :1 and :2 and the executor both, so only the registrar's recomputation refuses proofs
    naming :1 for a Consumer whose admitted selection is :2; nothing is bound. The same bind at the selection succeeds."""
    from nexloop_eios.effect_contexts import EffectContextRegistrar
    f = delivery_plan
    with monkeypatch.context() as m:
        m.setattr(EffectContextRegistrar, '_effect_version', lambda self, db, verb, payload: 1)
        with pytest.raises(Exception, match='unavailable'):
            f['bind']()
    assert admin.execute('select count(*) from control.nexloop_effect_contexts').fetchone() == (0,)
    f['bind']()
    assert admin.execute('select action_resource from control.nexloop_effect_contexts').fetchall() == [(f'eios:action:{EFFECT}:2',)]


def test_a_retirement_waits_for_a_bind_reading_the_admission(context_message, admin, pg):  # noqa: F811
    """L6 F2: the selection reads the active admissions for share until its transaction ends; a retirement locks the admission
    for update before its context check, so it waits for the bind and then sees the bind's context (refused, nothing retired)."""
    import threading
    import time
    f = context_message
    tenant = f['original']['tenant']
    bump_consumer_v2(admin, tenant)
    select = 'select authz.nexloop_effect_bind_action_resource(%s,%s,%s,%s)'
    outcome = {}
    with psycopg.connect(pg) as binder:
        with binder.transaction():
            assert binder.execute(select, (tenant, 'real', 'service.request:primary', 2)).fetchone()[0] == f'eios:action:{EFFECT}:2'
            # The bind's context naming :2 (as the registrar writes it), committed only after the retirement started waiting.
            binder.execute("""insert into control.nexloop_effect_contexts(context_id,tenant_id,world,consumer_id,goal_identity,goal_version_ref,plan_step_identity,
                slot_identity,consumer_revision,control_revision,allow_effect,valid_until,budget_units,reserved_units,expected_versions,executor_principal,action_resource)
                values(gen_random_uuid(),%s,'real','synthetic-consumer','g','goal:g@1','p','service.request:primary',1,1,true,clock_timestamp()+interval '5 minutes',
                1,0,'[1]','synthetic-executor',%s)""", (tenant, f'eios:action:{EFFECT}:2'))

            def retire():
                with psycopg.connect(pg, autocommit=True) as configurator:
                    configurator.execute('set session authorization nexloop_configurator')
                    outcome['result'] = configurator.execute("select control.nexloop_retire_effect_action_admission(%s,%s,2,'human:owner','合成退役')",
                                                             (tenant, EFFECT)).fetchone()[0]
            worker = threading.Thread(target=retire)
            worker.start()
            deadline = time.monotonic() + 10
            while not admin.execute("""select count(*) from pg_stat_activity where wait_event_type='Lock'
                    and query like '%%nexloop_retire_effect_action_admission%%'""").fetchone()[0]:
                assert time.monotonic() < deadline and worker.is_alive(), 'the retirement did not wait for the bind'
                time.sleep(0.05)
            assert 'result' not in outcome
        worker.join(10)
    assert outcome['result'] == {'retired': False, 'admission_id': None, 'code': 'referenced_by_unexpired_context'}
    assert admin.execute(select, (tenant, 'real', 'service.request:primary', 2)).fetchone()[0] == f'eios:action:{EFFECT}:2'


def test_the_admission_table_states_why_no_erasure_reaches_it(admin):
    """L6 note: the erasure basis is recorded on the table - role labels only (the column checks enforce it), never a member identity."""
    from nexloop_eios.bootstrap import bootstrap
    bootstrap(admin)
    comment = admin.execute("select obj_description('control.nexloop_effect_action_admissions'::regclass,'pg_class')").fetchone()[0]
    assert 'role labels only' in comment and 'never a member identity' in comment
    checks = [r[0] for r in admin.execute("""select pg_get_constraintdef(oid) from pg_constraint where conrelid='control.nexloop_effect_action_admissions'::regclass
        and contype='c' and pg_get_constraintdef(oid) ~ '(admitted|retired)_by ~'""").fetchall()]
    assert len(checks) == 2 and all('human:(owner|operator)' in c for c in checks), checks


def test_g1_owner_read_guard_rejects_hidden_or_nonzero_admissions(context_message, admin, pg):
    """Actual FORCE RLS, non-superuser owner, and the run sheet SQL: missing GUC is a false zero,
    active admissions must stop the gate, an empty synthetic tenant passes, application ACL errors never mean zero."""
    from pathlib import Path
    import re
    f = context_message
    tenant = f['original']['tenant']
    bump_consumer_v2(admin, tenant)
    assert admin.execute('select count(*) from control.nexloop_effect_action_admissions where retired_at is null').fetchone()[0] == 1
    with psycopg.connect(pg, autocommit=True) as db:
        with db.transaction():
            db.execute('set local role nexloop_owner')
            db.execute("select set_config('eios.tenant_id','',true)")
            assert db.execute('select count(*) from control.nexloop_effect_actions').fetchone()[0] > 0
            assert db.execute('select count(*) from control.nexloop_effect_action_admissions').fetchone()[0] == 0
        text = (Path(__file__).resolve().parents[1] / 'docs/release/run-task-b-m2-1.md').read_text()
        script = re.search(r'```sql\n(.*?)```', text, re.S)[1]
        def run(tenant_id):
            # psql variable quoting is represented by the driver's SQL literal, never string interpolation of input.
            from psycopg import sql
            statements = script.split('    do $guard$')
            before, after = statements[0], statements[1]
            guard, tail = after.split('end $guard$;', 1)
            try:
                for statement in before.split(';'):
                    if statement.strip():
                        db.execute(sql.SQL(statement.replace(":'tenant_id'", '{}')).format(sql.Literal(tenant_id)))
                db.execute('do $guard$' + guard + 'end $guard$;')
                return db.execute(tail.strip().removesuffix('rollback;').strip()).fetchone()
            finally:
                db.execute('rollback')
        with pytest.raises(psycopg.errors.RaiseException, match='active admissions present'):
            run(tenant)
        with pytest.raises(psycopg.errors.RaiseException, match='owner / tenant'):
            run('')
        result = run('synthetic-g1-empty-tenant')
        assert result[0:2] == ('nexloop_owner', 'synthetic-g1-empty-tenant') and result[2] > 0 and result[3] == 0
        with db.transaction():
            db.execute('set local role nexloop_api')
            db.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with db.transaction():
                    db.execute('select count(*) from control.nexloop_effect_action_admissions')
