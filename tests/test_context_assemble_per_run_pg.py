"""NX-023 (0104): nexloop.context.assemble:1 is issued with the Run, never standing.

The v6 Source of test_context_v6_pg holds no standing assemble grant. Its strategy read
and v6 bind carry the Run credential it issued; SQL accepts that only while the Run is
active, unexpired and not ended, for this Source credential, tenant and world, and (for
the bind) only for the pack's own Run. Admin connections only inject expiry/revocation
and terminal task states and probe. Synthetic data only.
"""
import hashlib,hmac,json
from types import SimpleNamespace

import psycopg,pytest
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.context_artifacts import ContextV6ArtifactProducer
from nexloop_eios.context_engine import strategy as S
from nexloop_eios.context_engine.authority import READ_PROTOCOL,ContextDenied,run_assemble_claims
from nexloop_eios.message_relay import MessageRelayUnavailable
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.run_credentials import AUDIENCE
from test_context_v6_pg import v6,bound  # noqa: F401
from test_context_artifacts import context_message  # noqa: F401
from local_message_assembly_fixture import assembled_message,business_plan,configured  # noqa: F401

STRATEGY_ID='recent_plus_required'
EFFECT='eios:action:nexloop.service.request:1'


def parts(f):
    return f['source']._backend._pool,f['source']._session,f['source']._backend._signer


def issue(f):
    pool,source,_=parts(f)
    run=f['source'].issue_run_credential(action_resources=[EFFECT])
    return run,authenticate_service(pool,run.token,world='real',run_id=run.run_id,audience=AUDIENCE)


def strategy(f,run):
    pool,source,signer=parts(f)
    return S.StrategyRegistry(pool,source,signer,run=run).get(STRATEGY_ID)


def raw_strategy(f,claims,*,world='real',session=None):
    """authz.nexloop_context_read 'strategy' with explicit (possibly forged) claims, correctly signed."""
    pool,source,signer=parts(f);session=session or source
    body=canonical_payload({'verb':'strategy','strategy_id':STRATEGY_ID})
    text=canonical_payload({**claims,'protocol':READ_PROTOCOL,'key_id':signer.key_id,'parameters_digest':hashlib.sha256(body.encode()).hexdigest(),'read_proofs':[]})
    signature=hmac.new(signer.material,(READ_PROTOCOL+':'+text).encode(),'sha256').hexdigest()
    with pool.connection() as db,db.transaction():
        return db.execute('select authz.nexloop_context_read(%s,%s,%s,%s,%s)',(session.token_digest,world,text,signature,body)).fetchone()[0]


def test_strategy_read_needs_the_issued_run_and_ends_with_it(v6,admin):
    f=v6;pool,source,signer=parts(f)
    # No standing grant: the Source alone cannot read the strategy.
    with pytest.raises(ContextDenied):S.StrategyRegistry(pool,source,signer).get(STRATEGY_ID)
    run,run_session=issue(f)
    assert strategy(f,run_session).ref.startswith('context-strategy:'+STRATEGY_ID+'@')
    # Revoked Run: refused.
    admin.execute("update authz.nexloop_run_credentials set status='revoked' where run_id=%s",(run.run_id,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege,match='run assemble unavailable'):strategy(f,run_session)
    # Expired Run: refused (the session object is still in hand).
    run2,run2_session=issue(f)
    assert strategy(f,run2_session) is not None
    admin.execute("update authz.nexloop_run_credentials set issued_at=clock_timestamp()-interval '100 seconds',expires_at=clock_timestamp()-interval '1 second' where run_id=%s",(run2.run_id,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege,match='run assemble unavailable'):
        raw_strategy(f,{**run_assemble_claims(source,SimpleNamespace(run_context=run2_session.run_context,token_digest=run2_session.token_digest,
            expires_at=run2_session.expires_at)),'expires_at':admin.execute("select (clock_timestamp()+interval '20 seconds')::text").fetchone()[0].replace(' ','T')})


def test_ended_run_is_refused(v6,admin):
    """A Run whose runtime task has ended no longer carries assemble authority."""
    f=v6;pool,source,signer=parts(f)
    assert f['v6_relay']().run_once()=='queued'
    run_id,digest,expires=admin.execute('''select r.run_id::text,r.token_digest,r.expires_at from authz.nexloop_runtime_run_bindings b
        join authz.nexloop_run_credentials r on r.run_id=b.run_id''').fetchone()
    run=SimpleNamespace(run_context=SimpleNamespace(run_id=run_id),token_digest=digest,expires_at=expires)
    assert strategy(f,run) is not None  # enrolled, task pending: still the live Run
    admin.execute("update runtime.jobs set status='succeeded' where job_id=(select task_id from authz.nexloop_runtime_run_bindings where run_id=%s)",(run_id,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege,match='run assemble ended'):strategy(f,run)


def test_other_source_tenant_world_and_forged_claims_are_refused(v6,admin):
    f=v6;pool,source,signer=parts(f)
    run,run_session=issue(f)
    good=run_assemble_claims(source,run_session)
    assert raw_strategy(f,good)['strategy_ref'].startswith('context-strategy:')
    # Another service credential presenting this Source's Run: not the issuing Source.
    planner=f['planner']._session
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        raw_strategy(f,{**run_assemble_claims(planner,run_session)},session=planner)
    # The Run credential itself is not a Source.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):raw_strategy(f,good,session=run_session)
    # Another tenant / another world / another Run digest / unknown Run / stale directory / standing-style facts.
    for change in ({'tenant_id':'synthetic-b'},{'world':'simulation'},{'run_assemble':{**good['run_assemble'],'run_digest':'0'*64}},
                   {'run_assemble':{**good['run_assemble'],'run_id':'00000000-0000-4000-8000-000000000000'}},{'directory_hash':'0'*64},
                   {'run_assemble':{**good['run_assemble'],'extra':1}},{'facts':[{'kind':'grants'}]}):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):raw_strategy(f,{**good,**change})
    with pytest.raises(psycopg.errors.InsufficientPrivilege):raw_strategy(f,{**good,'world':'simulation'},world='simulation')
    # A Run issued by this Source but presented in another world is not this Run.
    admin.execute("update authz.nexloop_run_credentials set world='simulation' where run_id=%s",(run.run_id,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege,match='run assemble unavailable'):raw_strategy(f,good)


def test_v6_bind_with_another_runs_assemble_is_refused(v6,admin,monkeypatch):
    """The bind accepts only the pack's own Run, even from the same Source."""
    f=v6
    other,other_session=issue(f)
    real=ContextV6ArtifactProducer._v6_call
    def swapped(self,db,payload,proofs):
        self.run=other_session
        return real(self,db,payload,proofs)
    monkeypatch.setattr(ContextV6ArtifactProducer,'_v6_call',swapped)
    relay=f['v6_relay']()
    with pytest.raises(MessageRelayUnavailable):relay.run_once()
    assert relay._context_diagnostic[1]=='run assemble other Run'
    assert admin.execute('select count(*) from runtime.nexloop_context_artifact_bindings').fetchone()==(0,)
    assert admin.execute('select count(*) from runtime.nexloop_context_packs').fetchone()==(0,)
    # Positive control (the pack's own Run binds): every test in test_context_v6_pg runs without standing assemble.
