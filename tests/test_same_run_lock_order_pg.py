"""NX-049: same-Run authorize and effect_tool take their locks in one order.

Deterministic reproduction of the cycle seen on the deploy profile: an effect_tool request
holds the Run's execution lock after its first guard; an authorize of the same Run then
locks its rows and waits for that execution lock; the effect_tool continues into its
intent admission, which needs one of those rows exclusively. Before the fix PostgreSQL
aborts the authorize with 40P01 (3/3 runs). Now both paths take the Run's execution lock
(0039 key, reentrant; runtime_activation._execution_lock) before any row lock, so the
authorize waits before touching any row and both requests succeed. Synthetic data only.
"""
import threading,time

import psycopg
from nexloop_eios.effect_intents import EffectIntentPort
from nexloop_eios.runtime_activation import RuntimeActivationPort
from runtime_effect_fixture import runtime_effect_plan  # noqa: F401
from role_run_fixture import role_runtime_plan  # noqa: F401
from test_backend_lifecycle_capacity import renew_leases


def blocked_advisory_waiters(admin):
    return admin.execute("select count(*) from pg_locks where locktype='advisory' and not granted").fetchone()[0]


def test_same_run_authorize_and_effect_tool_never_deadlock(role_runtime_plan,admin,monkeypatch):
    plan=role_runtime_plan;renew_leases(plan)
    sqlstates=[];held=threading.Event();go=threading.Event();outcomes={};timing={}
    original_admission=EffectIntentPort._execute_in_transaction
    def paused_admission(self,db,*args,**kwargs):
        # The tool transaction now holds the Run's execution lock (taken by its first guard).
        held.set();assert go.wait(20)
        return original_admission(self,db,*args,**kwargs)
    original_execute=RuntimeActivationPort._execute
    def recording_execute(self,db,envelope,**kwargs):
        try:return original_execute(self,db,envelope,**kwargs)
        except psycopg.Error as error:
            sqlstates.append(error.sqlstate);raise
    monkeypatch.setattr(EffectIntentPort,'_execute_in_transaction',paused_admission)
    monkeypatch.setattr(RuntimeActivationPort,'_execute',recording_execute)
    def services():
        return plan['backend_worker'].authenticate(plan['worker_token'],world='real')
    def tool():
        try:
            receipt=services().runtime_effect_tool(activation_ref=plan['activations'][0],command=plan['commands'][0],
                tool_operation='submit',parameters={'message':'one business intent'})
            outcomes['tool']='ok' if receipt['receipt']['scope']=='effect_intent' else receipt
        except Exception as error:
            outcomes['tool']=type(error).__name__;sqlstates.append(getattr(error,'diagnosis',None) and error.diagnosis.get('sqlstate'))
    def authorize():
        started=time.monotonic()
        try:outcomes['authorize']='ok' if services().authorize_runtime_activation(activation_ref=plan['activations'][0],
                command=plan['commands'][0],operation='model')['authorized'] else 'denied'
        except Exception as error:outcomes['authorize']=type(error).__name__
        timing['authorize']=time.monotonic()-started
    first=threading.Thread(target=tool);first.start()
    assert held.wait(30),'tool request never reached its intent admission'
    second=threading.Thread(target=authorize);second.start()
    # Wait until the authorize of the same Run is blocked on an advisory lock held by the tool.
    deadline=time.monotonic()+10
    while blocked_advisory_waiters(admin)==0 and second.is_alive() and time.monotonic()<deadline:time.sleep(0.02)
    assert blocked_advisory_waiters(admin)==1,'authorize did not wait for the tool request'
    released=time.monotonic();go.set()
    first.join(60);second.join(60)
    assert not first.is_alive() and not second.is_alive()
    assert '40P01' not in sqlstates,(outcomes,sqlstates)
    assert outcomes=={'tool':'ok','authorize':'ok'},(outcomes,sqlstates)
    # The authorize waited only for the rest of the tool transaction (well inside 2 s).
    print('authorize waited',round(timing['authorize'],3),'s in total;',round(time.monotonic()-released,3),'s after release')



def test_authorize_takes_the_execution_lock_before_its_rows_and_not_the_tool_run_lock(role_runtime_plan,admin,pg):
    """Authorize waits for a holder of the Run's execution lock (an in-flight tool after its
    first guard) before locking any row, then succeeds; the tool-only Run lock never blocks it."""
    from psycopg.conninfo import make_conninfo
    plan=role_runtime_plan;renew_leases(plan);run_id=plan['commands'][0]['run_id']
    services=plan['backend_worker'].authenticate(plan['worker_token'],world='real')
    def authorize():
        started=time.monotonic()
        assert services.authorize_runtime_activation(activation_ref=plan['activations'][0],command=plan['commands'][0],operation='inspect')['authorized']
        return time.monotonic()-started
    baseline=authorize()
    with psycopg.connect(make_conninfo(pg,user='nexloop_api')) as tool,tool.transaction():
        tool.execute('select pg_advisory_xact_lock(hashtextextended(%s,0))',('nexloop-role-effect:'+run_id,))
        assert authorize()<baseline+1.0  # the tool-only Run lock does not serialize authorize
    with psycopg.connect(make_conninfo(pg,user='nexloop_api')) as tool,tool.transaction():
        tool.execute('select pg_advisory_xact_lock(hashtextextended(%s,0))',('nexloop-runtime-execution:'+run_id,))
        result={};waiter=threading.Thread(target=lambda:result.update(seconds=authorize()));waiter.start()
        deadline=time.monotonic()+10
        while blocked_advisory_waiters(admin)==0 and time.monotonic()<deadline:time.sleep(0.02)
        assert blocked_advisory_waiters(admin)==1 and waiter.is_alive()
        # Waiting before its first row lock: the authorize holds no row lock yet.
        held=admin.execute("""select count(*) from pg_locks l where l.pid=(select pid from pg_locks where locktype='advisory' and not granted)
            and l.locktype='relation' and l.mode='RowShareLock'""").fetchone()[0]
        assert held==0,held
        time.sleep(0.3)
    waiter.join(30)
    assert not waiter.is_alive() and result['seconds']>=0.3
