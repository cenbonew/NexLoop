"""ADR-029a on clean catalog PostgreSQL: one message's context preparation (snapshot, bind, bind recheck after the write) reuses
the Source-signed proofs it built once, and the snapshot's catalog read uses the same object READ envelopes.

- Equivalence: the bound v6 pack equals, byte for byte (canonical form), the pack a preparation without any reuse assembles for
  the same message, Run and command.
- Counter-examples: the Source's authority withdrawn or the catalog changed after the proofs were built -> the bind reusing them
  is refused and nothing is bound; the same change attempted between the bind and its recheck cannot land (the binding
  transaction holds the rows it verified); the reused proofs about to expire before the recheck -> rebuilt, bound.
- Scope: nothing outlives one preparation; another preparation builds its proofs anew.
Synthetic data; the v6 relay fixture of test_context_v6_pg."""
import json

import pytest

from nexloop_eios import prepare_scope as P
from nexloop_eios.context_artifacts import ContextArtifactProducer, ContextArtifactUnavailable, ContextV6ArtifactProducer
from nexloop_eios.message_relay import MessageRelayUnavailable
from nexloop_eios.postgres_artifacts import canonical_payload
from test_context_artifacts import context_message  # noqa: F401
from test_context_v6_pg import bound, owner, v6  # noqa: F401
from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401


@pytest.fixture
def scopes(monkeypatch):
    """Every PrepareScope created, with what it built and reused."""
    made = []

    class Recorded(P.PrepareScope):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)
    monkeypatch.setattr(P, 'PrepareScope', Recorded)
    return made


def test_reused_proofs_bind_the_same_pack_as_a_preparation_without_reuse(v6, admin, scopes, monkeypatch):  # noqa: F811
    f = v6
    fresh = {}
    original = ContextV6ArtifactProducer._snapshot

    def snapshot(self, **arguments):
        result = original(self, **arguments)
        if self.scope is not None and not fresh:
            # The same message, Run and command prepared by a producer without any reuse (snapshot and assemble are reads).
            other = ContextV6ArtifactProducer(f['source'], 'recent_plus_required')
            other_result = original(other, **arguments)
            other.run = other_result[0]
            fresh['pack'] = canonical_payload(other.assemble(other_result[5], arguments['command'])[0])
            fresh['snapshot'] = canonical_payload(other_result[5])
            fresh['same_snapshot'] = canonical_payload(result[5]) == fresh['snapshot']
        return result
    monkeypatch.setattr(ContextV6ArtifactProducer, '_snapshot', snapshot)
    assert f['v6_relay']().run_once() == 'queued'
    pack, row = bound(admin)
    assert fresh['same_snapshot'] is True
    # Item decisions cite read proofs whose expiry is the time they were built (now + 25 s): a preparation without reuse builds
    # its own, so the two cite different decision refs for the same items. Everything else is byte for byte (0201: the pack now
    # carries the Consumer in consumer_state, the first item with a decision in this fixture).
    def without_decisions(text):
        value = json.loads(text)
        for section in ('constraints', 'consumer_state', 'open_work', 'evidence', 'semantics', 'experience'):
            for item in value[section]:
                item['access_decision_ref'] = 'decision:normalized'
        return canonical_payload(value)
    assert without_decisions(row[0]) == without_decisions(fresh['pack']) and canonical_payload(pack) == row[0]
    [scope] = scopes
    # Built once, used three times: the catalog READ envelopes (snapshot read + catalog envelope), the catalog envelope and the
    # message, Run, Action and Artifact proofs (snapshot, bind, recheck).
    assert scope.reused >= 2 * 6 and scope.built <= 8, (scope.built, scope.reused)
    kinds = sorted({key[0] for key in scope.items})
    assert kinds == ['action', 'artifact', 'catalog', 'message', 'object', 'run'], kinds


def between_bind_and_recheck(monkeypatch, change):
    """Runs `change` after the first bind and before the recheck (the claim finalization sits between them)."""
    original = ContextArtifactProducer._finalize

    def finalize(self, db, registrar, request, reservation, ref):
        original(self, db, registrar, request, reservation, ref)
        change(self)
    monkeypatch.setattr(ContextArtifactProducer, '_finalize', finalize)


def withdraw_source(admin, f):
    """The Source credential is revoked (committed outside the binding transaction)."""
    admin.execute("set lock_timeout='3s'")
    admin.execute("update authz.nexloop_service_credentials set status='revoked' where token_digest=encode(sha256(convert_to(%s,'UTF8')),'hex')", (f['source_token'],))


def change_catalog(admin, f):
    """The offering changes after the first bind (its revision moves on)."""
    tenant = f['original']['tenant']
    admin.execute("set lock_timeout='3s'")
    with admin.transaction():
        owner(admin, tenant)
        admin.execute("update ontology.objects set properties=properties||'{\"title\":\"合成：改动后的服务\"}'::jsonb,nexloop_revision=nexloop_revision+1 "
                      "where tenant_id=%s and object_id=%s", (tenant, f['f']['recipe']['offering_id']))


@pytest.mark.parametrize('change', [withdraw_source, change_catalog])
def test_a_change_after_the_proofs_were_built_refuses_the_bind_that_reuses_them(v6, admin, scopes, monkeypatch, change):  # noqa: F811
    """The snapshot built the proofs; the Source loses its authority, or the catalog changes, before the bind: the bind reusing
    them is refused by SQL and nothing is bound."""
    f = v6
    original = ContextV6ArtifactProducer.assemble
    ran = []

    def assemble(self, snapshot, command):
        change(admin, f)  # committed after the snapshot, before the bind transaction
        ran.append(self.scope.built)
        return original(self, snapshot, command)
    monkeypatch.setattr(ContextV6ArtifactProducer, 'assemble', assemble)
    relay = f['v6_relay']()
    with pytest.raises(MessageRelayUnavailable):
        relay.run_once()
    assert ran and ran[0] > 0
    assert relay._context_diagnostic is not None and relay._context_diagnostic[0] != 'LockNotAvailable', relay._context_diagnostic
    assert admin.execute('select count(*) from runtime.nexloop_context_artifact_bindings').fetchone() == (0,)
    with admin.transaction():
        owner(admin, f['original']['tenant'])
        assert admin.execute('select (select count(*) from runtime.nexloop_context_packs),(select count(*) from runtime.nexloop_context_sources)').fetchone() == (0, 0)
    [scope] = scopes
    assert scope.reused > 0  # the refused bind used proofs built before the change


@pytest.mark.parametrize('change', [withdraw_source, change_catalog])
def test_no_change_lands_between_the_bind_and_its_recheck(v6, admin, scopes, monkeypatch, change):  # noqa: F811
    """Between the bind and its recheck (one transaction) the binding holds the rows it verified: the revocation or catalog
    change waits for it (here: gives up at its lock timeout), so the recheck verifies the state the bind verified."""
    import psycopg
    f = v6
    blocked = []

    def attempt(producer):
        try:
            change(admin, f)
        except psycopg.errors.LockNotAvailable:
            blocked.append(True)
    between_bind_and_recheck(monkeypatch, attempt)
    assert f['v6_relay']().run_once() == 'queued'
    assert blocked == [True]
    assert admin.execute('select count(*) from runtime.nexloop_context_artifact_bindings').fetchone() == (1,)
    change(admin, f)  # lands once the binding committed


def test_reused_proofs_close_to_expiry_are_rebuilt_for_the_recheck(v6, admin, scopes, monkeypatch):  # noqa: F811
    f = v6
    seen = {}

    def age(producer):
        # 21 s later for this scope: every held proof (at most 25 s) has under 5 s left, so the recheck rebuilds them.
        scope = producer.scope
        seen['built_before'] = scope.built
        now = scope.clock()
        scope.clock = lambda: now + P.timedelta(seconds=21)
    between_bind_and_recheck(monkeypatch, age)
    assert f['v6_relay']().run_once() == 'queued'
    [scope] = scopes
    assert scope.built >= seen['built_before'] + 6, (seen, scope.built)
    assert admin.execute('select count(*) from runtime.nexloop_context_artifact_bindings').fetchone() == (1,)


def test_nothing_outlives_one_preparation(v6, admin, scopes):  # noqa: F811
    f = v6
    assert f['v6_relay']().run_once() == 'queued'
    tenant = f['original']['tenant']
    record = f['vault'].read(f['vault'].message_key(tenant, 'real', f['message']['id']))
    command = admin.execute('select normalized_input from runtime.jobs').fetchone()[0]['run_command']
    producer = ContextV6ArtifactProducer(f['source'], 'recent_plus_required')
    producer.prepare(message_id=f['message']['id'], run_token=record.token, command=command,
                     offering_id=f['f']['recipe']['offering_id'], binding_id=f['f']['recipe']['offering_binding_id'])
    assert producer.scope is None
    first, second = scopes
    assert first is not second and second.built > 0
    # The second preparation started empty: its first use of each proof was a build, never the first preparation's proof.
    for key, (value, _) in second.items.items():
        if key in first.items and key[0] in ('action', 'artifact', 'run', 'object', 'catalog'):
            assert value is not first.items[key][0]


def test_expiry_reads_every_nested_expiry_and_keeps_nothing_without_one():
    now = P.datetime.now(P.UTC)
    soon, later = (now + P.timedelta(seconds=s) for s in (6, 20))
    proof = {'expires_at': later.isoformat(), 'nested': [{'text': json.dumps({'expires_at': soon.isoformat()})}]}
    assert P.expiry(proof) == soon and P.expiry({'no': 'expiry'}) is None
    scope = P.PrepareScope(clock=lambda: now)
    calls = []
    assert scope.get('a', lambda: calls.append(1) or {'x': 1}) == {'x': 1} and scope.get('a', lambda: calls.append(1) or {'x': 1}) == {'x': 1}
    assert calls == [1, 1] and scope.items == {}  # no readable expiry: never kept
    scope.get('b', lambda: proof)
    assert scope.get('b', lambda: {'rebuilt': True}) is proof
    scope.clock = lambda: now + P.timedelta(seconds=2)  # 4 s left on the nested proof: rebuilt
    assert scope.get('b', lambda: {'rebuilt': True}) == {'rebuilt': True}
