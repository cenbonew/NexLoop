"""NX-049 means 5 and 4a: fewer round trips, same transactions, same authority outcomes.

5:  a single-query authority unit loads its predicted fact snapshots in its opening
    statement (with the live identity and the unit's trusted time) instead of one
    round trip per fact; outcomes, proof record hashes and expiry are unchanged.
4a: inside a backend request scope, idle checkouts reuse the request's own connection
    (nested work: one second connection); every block keeps its own transaction; the
    application-role check runs once per request connection; REPEATABLE READ READ ONLY
    travels in BEGIN and the connection defaults are restored.
Synthetic data only.
"""
import pytest
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
import nexloop_eios.authorization as A
import nexloop_eios.request_connection as R
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.authorization import authority_request_scope
from test_batch_authorities import single,query
from test_action_definitions import published_action  # noqa: F401
from test_identity_fact_memo import ALLOWED,TARGETS,varied  # noqa: F401


@pytest.fixture
def statements(monkeypatch):
    """Statements per authority unit: opening statement, prefetch batches and single-fact reads."""
    seen={'single':0,'batch':0,'opening_with_facts':0}
    original=A.PostgresAuthorityUnitOfWork._fetch
    def fetch(self,kind,key):
        if self._prefetched.get((kind,tuple(key)),False) is False:seen['single']+=1
        return original(self,kind,key)
    original_accept=A.PostgresAuthorityUnitOfWork.accept
    def accept(self,keys,rows):
        seen['batch']+=1;return original_accept(self,keys,rows)
    monkeypatch.setattr(A.PostgresAuthorityUnitOfWork,'_fetch',fetch)
    monkeypatch.setattr(A.PostgresAuthorityUnitOfWork,'accept',accept)
    return seen


def test_single_unit_prefetch_is_equivalent_and_removes_single_fact_reads(varied,statements,monkeypatch):
    reader,session,_=varied
    with_prefetch={t:single(reader.pool,session,t) for t in TARGETS}
    assert statements['batch']==len(TARGETS)  # one prefetch per unit, inside its opening statement
    assert statements['single']==0             # every fact the resolver read was predicted
    with monkeypatch.context() as patch:
        patch.setattr(A.PostgresAuthorityProvider,'prefetch_on_open',False)
        without={t:single(reader.pool,session,t) for t in TARGETS}
    assert with_prefetch==without               # outcome, authoritative flag, expiry, record hashes
    assert [with_prefetch[t][0] for t in ALLOWED]==[True,True]
    # Inside one request scope, identity facts memoized by O2a are not fetched again.
    statements.update(single=0,batch=0)
    with authority_request_scope():
        scoped=[single(reader.pool,session,t) for t in ALLOWED]
    assert scoped==[with_prefetch[t] for t in ALLOWED] and statements['single']==0


def _pid(connection):return connection.execute('select pg_backend_pid()').fetchone()[0]
def _xid(connection):return connection.execute('select pg_current_xact_id()::text').fetchone()[0]


def test_request_scope_reuses_its_connection_and_keeps_every_transaction(varied):
    reader,_,_=varied;facade=R.RequestConnectionPool(reader.pool)
    with R.request_connection_scope(facade):
        with facade.connection() as a,a.transaction():first=(_pid(a),_xid(a))
        with facade.connection() as b,b.transaction():second=(_pid(b),_xid(b))
        assert first[0]==second[0] and first[1]!=second[1]  # same connection, separate transactions
        with facade.connection() as outer,outer.transaction():
            with facade.connection() as nested,nested.transaction():
                nested_pid=_pid(nested)
                assert nested_pid!=_pid(outer)              # nested work: the second connection
                with facade.connection() as deeper:
                    assert _pid(deeper) not in (_pid(outer),nested_pid)  # beyond budget: separate, as before
        with facade.connection() as outer,outer.transaction():
            with facade.connection() as nested:assert _pid(nested)==nested_pid  # the second is reused
        # An implicit transaction left open is committed at block end; an error rolls back.
        with facade.connection() as c:implicit=_xid(c)
        with facade.connection() as c:assert _xid(c)!=implicit
        with pytest.raises(ZeroDivisionError):
            with facade.connection() as c:_xid(c);1/0
        with facade.connection() as c:assert c.info.transaction_status.name=='IDLE' and _pid(c)==first[0]
    stats=reader.pool.get_stats()
    assert stats['pool_available']==stats['pool_size']  # nothing left checked out
    # Outside a scope the facade is the plain pool.
    with facade.connection() as c:assert c.info.transaction_status.name=='IDLE'


def test_role_check_once_per_request_connection(varied,monkeypatch):
    reader,_,_=varied;facade=R.RequestConnectionPool(reader.pool);checks=[]
    original=R.remember_role
    monkeypatch.setattr(R,'remember_role',lambda connection,role:(checks.append(_pid(connection)),original(connection,role)))
    with R.request_connection_scope(facade):
        for _ in range(3):
            with facade.connection() as c:assert verify_application_role(c)=='nexloop_api'
        with facade.connection() as outer,outer.transaction():
            for _ in range(2):
                with facade.connection() as nested:verify_application_role(nested)
    assert len(checks)==2 and len(set(checks))==2  # once per request connection
    with R.request_connection_scope(facade):
        with facade.connection() as c:verify_application_role(c)
    assert len(checks)==3                          # the next request checks again
    with facade.connection() as c:verify_application_role(c);verify_application_role(c)
    assert len(checks)==5                          # outside a request: every call


def test_unit_isolation_travels_in_begin_and_connection_defaults_are_restored(varied):
    reader,session,_=varied;facade=R.RequestConnectionPool(reader.pool)
    provider=A.PostgresAuthorityProvider(facade,session)
    with R.request_connection_scope(facade):
        with provider.open_unit_of_work(query(session,ALLOWED[0])) as unit:
            assert unit.connection.execute('show transaction_isolation').fetchone()==('repeatable read',)
            assert unit.connection.execute('show transaction_read_only').fetchone()==('on',)
            pid=_pid(unit.connection)
        with facade.connection() as c,c.transaction():
            assert _pid(c)==pid and (c.isolation_level,c.read_only)==(None,None)
            assert c.execute('show transaction_isolation').fetchone()==('read committed',)
            assert c.execute('show transaction_read_only').fetchone()==('off',)
