"""Actual governed recovery rejects skipped locked-row terminal writes atomically."""
import pytest,psycopg
from role_run_fixture import role_runtime_plan
from runtime_effect_fixture import runtime_effect_plan
from nexloop_eios.effect_execution import EffectExecutionPort
from test_receipt_tail import test_current_recovery_definition_and_nested_tail as _normal_tail_chain

@pytest.mark.parametrize('target',['original_action','effect_attempt'])
def test_skipped_original_or_effect_terminal_update_rolls_back_all(role_runtime_plan,admin,tmp_path,monkeypatch,target):
    execute=EffectExecutionPort._recovery_execute;captured={}
    def inject(self,db,verb,resource,**parameters):
        if verb!='recover':return execute(self,db,verb,resource,**parameters)
        intent=parameters['intent_id'];captured['intent']=intent
        captured['claim']=admin.execute("select claim from runtime.nexloop_action_claims where action_name='nexloop.service.request' and intent_id=%s",(intent,)).fetchone()[0]
        for table in ('nexloop_effect_attempts','nexloop_effect_intents','nexloop_effect_outbox'):
            captured[table]=admin.execute('select to_jsonb(r) from runtime.'+table+' r where intent_id=%s',(intent,)).fetchone()[0]
        if target=='original_action':
            admin.execute("create function runtime.synthetic_skip_locked_terminal() returns trigger language plpgsql as $$begin if new.action_name='nexloop.service.request' and new.claim->>'state'='terminal' then return null;end if;return new;end$$")
            table='nexloop_action_claims';expected='original action terminal row unavailable'
        else:
            admin.execute("create function runtime.synthetic_skip_locked_terminal() returns trigger language plpgsql as $$begin if new.state='fulfilled' then return null;end if;return new;end$$")
            table='nexloop_effect_attempts';expected='effect attempt terminal row unavailable'
        # Owned disposable technical fault-hook metadata only. Actual original
        # POST/Role.end/GET/observation and recovery use genuine governed Actions.
        admin.execute('create trigger synthetic_skip_locked_terminal before update on runtime.'+table+' for each row execute function runtime.synthetic_skip_locked_terminal()')
        try:return execute(self,db,verb,resource,**parameters)
        except psycopg.Error as error:
            assert error.diag.message_primary==expected
            captured['actual_refusal']=expected
            raise
    monkeypatch.setattr(EffectExecutionPort,'_recovery_execute',inject)
    _normal_tail_chain(role_runtime_plan,admin,tmp_path,monkeypatch,'skipped_original_or_effect_terminal')
    intent=captured['intent']
    assert captured['actual_refusal']
    assert admin.execute("select claim from runtime.nexloop_action_claims where action_name='nexloop.service.request' and intent_id=%s",(intent,)).fetchone()[0]==captured['claim']
    for table in ('nexloop_effect_attempts','nexloop_effect_intents','nexloop_effect_outbox'):
        assert admin.execute('select to_jsonb(r) from runtime.'+table+' r where intent_id=%s',(intent,)).fetchone()[0]==captured[table]
    assert admin.execute("select count(*) from runtime.nexloop_action_claims where action_name='nexloop.service.receipt_reconcile'").fetchone()==(0,)
    assert admin.execute('select count(*) from runtime.nexloop_effect_recovery_audits where intent_id=%s',(intent,)).fetchone()==(0,)
