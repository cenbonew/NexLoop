"""Current recovery grant revoked while SQL waits before taking authority locks."""
import time
from concurrent.futures import ThreadPoolExecutor
import psycopg
from role_run_fixture import role_runtime_plan
from runtime_effect_fixture import runtime_effect_plan
from nexloop_eios.effect_execution import EffectExecutionPort
from authority_fixture import replace_fact
from eios.authz import facts as F
from test_receipt_tail import test_current_recovery_definition_and_nested_tail as _normal_tail_chain


def test_recovery_grant_revoked_during_actual_signing_key_lock_wait(role_runtime_plan,admin,tmp_path,monkeypatch):
    plan=role_runtime_plan;execute=EffectExecutionPort._recovery_execute;observed=[]
    def wait_revoke(self,db,verb,target,**parameters):
        if verb!='recover':return execute(self,db,verb,target,**parameters)
        with psycopg.connect(plan['pg']) as blocker,ThreadPoolExecutor(max_workers=1) as executor:
            blocker.execute('select key_id from authz.nexloop_authority_signing_keys where key_id=%s for update',(plan['signing_key_id'],))
            future=executor.submit(execute,self,db,verb,target,**parameters)
            try:
                deadline=time.monotonic()+2;waiting=0
                while time.monotonic()<deadline:
                    waiting=admin.execute("select count(*) from pg_stat_activity where usename='nexloop_action_worker' and wait_event_type='Lock' and query like 'select authz.nexloop_receipt_reconcile_command%'").fetchone()[0]
                    if waiting:break
                    time.sleep(.02)
                assert waiting
                # Permission metadata fixture only; no direct business/receipt writes.
                replace_fact(admin,self.session.authentication.tenant_id,'grants',
                    [self.session.authentication.subject_principal_id,'eios:action:nexloop.service.receipt_reconcile:1'],F.GrantFacts,grants=[])
                observed.append('actual_lock_then_revocation')
            finally:blocker.commit()
            return future.result(timeout=5)
    monkeypatch.setattr(EffectExecutionPort,'_recovery_execute',wait_revoke)
    # Reuses actual governed POST→Role.end→GET→durable typed observation and
    # asserts failed recovery rolled back original terminal/receipt/selfclaim/audit.
    _normal_tail_chain(role_runtime_plan,admin,tmp_path,monkeypatch,'revocation_wait')
    assert observed==['actual_lock_then_revocation']
