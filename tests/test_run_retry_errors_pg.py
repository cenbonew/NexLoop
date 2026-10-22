"""Stage 2026-10-22 (07:16-07:19Z): two messages per conversation ~10 s apart left 'context v6 core mismatch' and four
'artifact idempotency payload conflict' ERRORs although every message was answered.

1. Another Run's reservation raises the shared EffectControl ledger's reserved_units between a message's Context snapshot and its
   bind: the bind no longer refuses that (0317) - every other core field still has to match.
2. A Run whose Context Artifact an earlier attempt stored but never bound cannot bind on a retry (the Artifact is the Run's one
   Context and a new pack carries new read decisions): the relay hands the message to the reply guarantee at once instead of
   spending its remaining attempts, each one a payload conflict.
Synthetic data only; the admin connection only injects the concurrent change and reads the server log.
"""
from pathlib import Path

import pytest
from psycopg.conninfo import conninfo_to_dict
from nexloop_eios.context_artifacts import ContextV6ArtifactProducer
from nexloop_eios.message_relay import MessageRelayUnavailable
from test_context_v6_pg import v6,bound
from test_context_artifacts import context_message
from local_message_assembly_fixture import assembled_message,business_plan,configured


def server_log(pg):
    """The test cluster's server log (tests/conftest.py: <root>/postgres.log beside <root>/socket)."""
    path=Path(conninfo_to_dict(pg)['host']).parent/'postgres.log'
    start=len(path.read_text(errors='replace'))
    return lambda:[l for l in path.read_text(errors='replace')[start:].splitlines() if 'ERROR:' in l]


def before_bind(monkeypatch,change):
    real=ContextV6ArtifactProducer._v6_call;calls=[]
    def injected(self,*a,**kw):
        calls.append(1)
        change(len(calls))
        return real(self,*a,**kw)
    monkeypatch.setattr(ContextV6ArtifactProducer,'_v6_call',injected)


def ledger(admin,column):
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute(f'update control.nexloop_effect_control_ledger set {column}={column}+1')


def test_a_reservation_between_snapshot_and_bind_no_longer_refuses_the_bind(v6,admin,pg,monkeypatch):
    errors=server_log(pg)
    before_bind(monkeypatch,lambda n:ledger(admin,'reserved_units') if n==1 else None)  # another Run's reply reserved meanwhile
    assert v6['v6_relay']().run_once()=='queued'
    pack,_=bound(admin)
    assert pack['current_constraints']['reserved_units']+1==admin.execute('select reserved_units from control.nexloop_effect_control_ledger').fetchone()[0]
    assert errors()==[]


def test_any_other_core_change_still_refuses_the_bind(v6,admin,pg,monkeypatch):
    errors=server_log(pg)
    before_bind(monkeypatch,lambda n:ledger(admin,'budget_units') if n==1 else None)
    relay=v6['v6_relay']()
    with pytest.raises(MessageRelayUnavailable):relay.run_once()
    assert relay._diagnostic_failure==('context_prepare','ContextArtifactUnavailable') and errors()  # refused (the plan check sees it first)


def test_a_retry_of_a_run_whose_context_was_stored_but_never_bound_is_handed_off_at_once(v6,admin,pg,monkeypatch):
    errors=server_log(pg)
    def lost(n):
        if n==1:raise RuntimeError('bind lost')  # the first attempt stored the Artifact, then failed before its bind
    before_bind(monkeypatch,lost)
    relay=v6['v6_relay']()
    with pytest.raises(MessageRelayUnavailable):relay.run_once()
    with admin.transaction():admin.execute('update runtime.nexloop_message_relay_leases set lease_until=clock_timestamp()')
    relay=v6['v6_relay']()
    assert relay.run_once()=='handed_to_fallback' and relay.last_handoff=='binding_conflict'
    assert admin.execute('select count(*) from runtime.nexloop_context_artifact_bindings').fetchone()==(0,)
    assert [l.split('ERROR:')[1].strip() for l in errors()]==['artifact idempotency payload conflict']  # once, not once per attempt
    assert v6['v6_relay']().run_once()=='idle'  # the message has left the relay
