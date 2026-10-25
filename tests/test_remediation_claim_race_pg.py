"""NX-057 R4 (migration remediation_claims; L6 R4 review R-1, S-1, S-3): the action claim of an effect intent is closed by a final
failure in the failure's own transaction, and a concurrent finalize or retryable of the same claim is serialized with it.

Real PostgreSQL. The claim is a real one: reserved, finalized and marked retryable through the production claim port and SQL function
(authz.nexloop_action_claim_command), keyed as the effect executor keys it - the Action's stable name and the intent id (S-3; the
executor's own key is asserted in tests/test_effect_dispatch.py). The failure row is written by admin as the three failure paths write
it. The race is ordered by PostgreSQL's lock queue, not by sleeping: a third session holds the claim command's advisory lock, each side
is started only after pg_locks shows the one before it waiting on that lock, and the lock is then released in queue order.
"""
import secrets
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from eios.actions import models as M, ports as P
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.ontology.definitions import CapabilityBinding, DefinitionReference, DefinitionType
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from authority_fixture import seed_authority
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.postgres_action_claims import PostgresActionClaimPort
from nexloop_eios.postgres_artifacts import AuthoritySigner
from support.tenants import TENANT_A

ACTION = 'synthetic.send'


@pytest.fixture
def claims(admin, pg):
    bootstrap(admin)
    token, _ = seed_authority(admin, TENANT_A, f'eios:action:{ACTION}:1', operation=Operation.EXECUTE, resource_type=ResourceType.ACTION)
    signer = AuthoritySigner('synthetic-race', secrets.token_bytes(32))
    admin.execute('insert into authz.nexloop_authority_signing_keys values(%s,%s,true)', (signer.key_id, signer.material))
    with open_core(make_conninfo(pg, user='nexloop_api')) as pool:
        yield PostgresActionClaimPort(pool, authenticate_service(pool, token, world='real'), signer)


def failed_intent(admin):
    """An effect intent already failed (as the failure paths leave it), with no failure row yet."""
    intent = uuid.uuid4()
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')  # synthetic row: its context and Run are not seeded
        admin.execute("""insert into runtime.nexloop_effect_intents(intent_id,receipt_id,context_id,tenant_id,world,consumer_id,goal_identity,
            plan_step_identity,slot_identity,action_name,action_version,business_digest,provider_payload_digest,frozen_request,action_definition,
            capability,executor_principal,control_revision,created_at,state,governed_claim_finalized) values(%s,%s,%s,%s,'real','consumer-synthetic',
            %s,%s,%s,%s,1,%s,%s,%s,%s,%s,'executor-synthetic',1,clock_timestamp(),'failed',false)""",
            (intent, uuid.uuid4(), uuid.uuid4(), TENANT_A, 'goal-' + intent.hex[:8], 'step-' + intent.hex[:8], 'slot-' + intent.hex[:8], ACTION,
             'a' * 64, 'b' * 64, Jsonb({}), Jsonb({}), Jsonb({})))
    return intent


def reserve(port, intent):
    now = datetime.now(UTC)
    ref = DefinitionReference(tenant_id=TENANT_A, definition_type=DefinitionType.ACTION, stable_name=ACTION, version=1, contract_digest='a' * 64)
    binding = M.ClaimBindingPayload(invocation_id=str(intent), action_reference=ref, request_digest='b' * 64,
                                    capability_binding=CapabilityBinding(capability_name='synthetic.send', capability_version='1.0.0', schema_hash='c' * 64),
                                    adapter_id='nexloop.core', target_system='postgres')
    result = port.reserve(M.ActionClaimRequest(key=M.ActionReservationKey(tenant_id=TENANT_A, action_stable_name=ACTION, idempotency_key=str(intent)),
                                               binding=binding, requested_at=now, lease_expires_at=now + timedelta(seconds=60)))
    assert result.disposition is M.ActionClaimDisposition.CLAIMED
    return result.claim


def finalize_command(claim):
    return M.ActionClaimFinalizeCommand(key=claim.key, binding=claim.binding, expected_claim_revision=claim.claim_revision, fencing_token=claim.fencing_token,
                                        outcome=M.TerminalOutcomeReference(outcome_id='synthetic-receipt', outcome_revision=1, status=M.TerminalOutcomeStatus.SUCCEEDED,
                                                                           outcome_digest='d' * 64, finalized_at=datetime.now(UTC)))


def retryable_command(claim):
    return M.ActionClaimRetryableCommand(key=claim.key, binding=claim.binding, expected_claim_revision=claim.claim_revision,
                                         fencing_token=claim.fencing_token, marked_at=datetime.now(UTC))


def lock_key(intent):
    return f'nexloop-action:{TENANT_A}:real:{ACTION}:{intent}'


def waiting(admin):
    return admin.execute("select count(*) from pg_locks where locktype='advisory' and not granted").fetchone()[0]


def until(predicate, seconds=20):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, 'barrier not reached'
        time.sleep(0.02)  # polling cadence only; the order comes from the lock queue read in pg_locks


def write_failure(pg, intent):
    with psycopg.connect(pg) as c:  # the failure path's own transaction (committed on leaving the block)
        c.execute("insert into runtime.nexloop_effect_failures(intent_id,tenant_id,world,reason_code,evidence) values(%s,%s,'real','origin_expired','{}')",
                  (intent, TENANT_A))


def race(admin, pg, first, second):
    """Run first, then second, each blocked on the claim's advisory lock held by a third session, released in queue order."""
    results = {}

    def run(name, fn):
        try:
            results[name] = fn()
        except Exception as error:  # noqa: BLE001 - the expected refusals are asserted by the caller
            results[name] = error
    with psycopg.connect(pg) as holder:
        holder.execute('select pg_advisory_xact_lock(hashtextextended(%s,0))', (first[2],))
        threads = []
        for n, (name, fn, _) in enumerate((first, second), start=1):
            t = threading.Thread(target=run, args=(name, fn));t.start();threads.append(t)
            until(lambda n=n: waiting(admin) == n)  # this side waits in the queue before the next one starts
        holder.commit()
    for t in threads:
        t.join(30)
        assert not t.is_alive()
    return results


def claim_row(admin, intent):
    return admin.execute('select claim from runtime.nexloop_action_claims where action_name=%s and intent_id=%s', (ACTION, str(intent))).fetchone()[0]


def test_a_finalize_committed_first_wins_and_the_failure_leaves_it(claims, admin, pg):
    intent = failed_intent(admin);claim = reserve(claims, intent)
    out = race(admin, pg, ('finalize', lambda: claims.finalize(finalize_command(claim)), lock_key(intent)),
               ('failure', lambda: write_failure(pg, intent), lock_key(intent)))
    assert out['failure'] is None and out['finalize'].state is M.ActionClaimState.TERMINAL
    row = claim_row(admin, intent)
    assert row['terminal_outcome']['status'] == 'succeeded' and row['terminal_outcome']['outcome_id'] == 'synthetic-receipt'
    assert admin.execute('select count(*) from runtime.nexloop_effect_failures where intent_id=%s', (intent,)).fetchone() == (1,)


@pytest.mark.parametrize('late', ['finalize', 'retryable'])
def test_a_failure_committed_first_wins_and_a_late_finalize_or_retryable_is_refused(claims, admin, pg, late):
    intent = failed_intent(admin);claim = reserve(claims, intent)
    command = (lambda: claims.finalize(finalize_command(claim))) if late == 'finalize' else (lambda: claims.mark_retryable(retryable_command(claim)))
    out = race(admin, pg, ('failure', lambda: write_failure(pg, intent), lock_key(intent)), (late, command, lock_key(intent)))
    assert out['failure'] is None
    expected = (P.ActionClaimOutcomeConflictError, 'action_claim_outcome_conflict') if late == 'finalize' else \
        (P.ActionClaimTransitionError, 'action_claim_transition_invalid')
    assert isinstance(out[late], expected[0]) and out[late].code == expected[1], repr(out[late])
    row = claim_row(admin, intent)
    failure = admin.execute('select failure_id::text from runtime.nexloop_effect_failures where intent_id=%s', (intent,)).fetchone()[0]
    assert row['state'] == 'terminal' and row['terminal_outcome']['status'] == 'permanent_failure' and row['terminal_outcome']['outcome_id'] == failure
    assert row['claim_revision'] == claim.claim_revision and row['fencing_token'] == claim.fencing_token
    # The closed claim keeps the frozen contract: a replay of the reservation reads it as a valid terminal outcome.
    replay = claims.reserve(M.ActionClaimRequest(key=claim.key, binding=claim.binding, requested_at=datetime.now(UTC),
                                                 lease_expires_at=datetime.now(UTC) + timedelta(seconds=60)))
    assert replay.disposition is M.ActionClaimDisposition.REPLAY
    assert replay.terminal_outcome.status is M.TerminalOutcomeStatus.PERMANENT_FAILURE and replay.terminal_outcome.outcome_id == failure
