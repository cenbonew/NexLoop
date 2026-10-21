"""Signature denial causes (0276; stage 2026-10-20: one message stood for six different failures). Each signed-command gate keeps its
errcode 42501 and its message - clients still decide on the message - and gives the failed condition as DETAIL (key_unknown, protocol,
resource, expired, too_far_ahead, digest, signature); for too_far_ahead the HINT gives the excess over the bound (0.1 s). No value of
the request, no key and no parameter is in DETAIL or HINT.

Real PostgreSQL: the durable queue through its client (every cause, the normal path as positive control), the effect execution, the
runtime activation and the context v6 gates called with a signed proof altered in exactly one way."""
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import secrets

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from authority_fixture import seed_authority
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios import durable_queue, effect_execution
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.durable_queue import PostgresDurableQueue
from nexloop_eios.postgres_artifacts import AuthoritySigner, canonical_payload
from support.tenants import TENANT_A

QUEUE_TARGET = 'eios:action:NexLoop.queue.operations:1'
CAUSES = ('key_unknown', 'protocol', 'resource', 'expired', 'too_far_ahead', 'digest', 'signature')


def iso(seconds):
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def signer(admin):
    bootstrap(admin)
    s = AuthoritySigner('synthetic-denial', secrets.token_bytes(32))
    admin.execute('insert into authz.nexloop_authority_signing_keys values(%s,%s,true)', (s.key_id, s.material))
    return s


@pytest.fixture
def queue(signer, admin, pg):
    with ExitStack() as stack:
        token, _ = seed_authority(admin, TENANT_A, QUEUE_TARGET, world='real', operation=Operation.EXECUTE, resource_type=ResourceType.ACTION,
                                  identity_suffix='-denial-api')
        pool = stack.enter_context(open_core(make_conninfo(pg, user='nexloop_api')))
        yield PostgresDurableQueue(pool, authenticate_service(pool, token, world='real'), signer, queue='operations')


def altered(monkeypatch, change):
    """Let the queue client build and sign its claims as always, with one claim changed before signing (the signature stays valid)."""
    original = durable_queue.canonical_payload

    def patched(value):
        if isinstance(value, dict) and value.get('protocol') == 'nexloop-queue-command-v1':
            value = {**value, **change}
        return original(value)
    monkeypatch.setattr(durable_queue, 'canonical_payload', patched)


def refused(call):
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as error:
        call()
    return error.value.diag


@pytest.mark.parametrize('cause', CAUSES)
def test_each_queue_denial_names_its_cause(queue, signer, monkeypatch, cause):
    accept = lambda: queue.accept(source_id='denial', event_id=cause, payload={'text': 'synthetic'})  # noqa: E731
    if cause == 'key_unknown':
        queue.signer = AuthoritySigner('synthetic-unknown-key', secrets.token_bytes(32))
    elif cause == 'signature':
        queue.signer = AuthoritySigner(signer.key_id, secrets.token_bytes(32))  # the right key id, another key
    else:
        altered(monkeypatch, {'protocol': {'protocol': 'nexloop-queue-command-v0'}, 'resource': {'resource_id': 'eios:action:NexLoop.queue.other:1'},
                              'expired': {'expires_at': iso(-1)}, 'too_far_ahead': {'expires_at': iso(40)},
                              'digest': {'parameters_digest': '0' * 64}}[cause])
    diag = refused(accept)
    # The message and errcode are unchanged: clients keep deciding on them.
    assert diag.message_primary == 'queue signature denied' and diag.sqlstate == '42501'
    assert diag.message_detail == cause
    if cause == 'too_far_ahead':
        assert diag.message_hint.startswith('ahead_by_seconds=') and 14.0 <= float(diag.message_hint.split('=')[1]) <= 15.1, diag.message_hint
        assert len(diag.message_hint.split('=')[1].split('.')[1]) == 1  # rounded to 0.1 s
    else:
        assert diag.message_hint in (None, '')
    # Nothing of the request is in DETAIL or HINT.
    assert signer.key_id not in (diag.message_detail or '') + (diag.message_hint or '') and 'synthetic' not in (diag.message_detail or '')


def test_the_normal_queue_path_is_unchanged(queue):
    assert queue.accept(source_id='denial', event_id='normal', payload={'text': 'synthetic'})['accepted'] is True  # positive control


def signed(material, protocol, claims):
    text = canonical_payload(claims)
    return text, hmac.new(material, (protocol + ':' + text).encode(), 'sha256').hexdigest()


def test_effect_execution_denials_name_their_cause(signer, admin, pg):
    target = 'eios:action:nexloop.service.request:1'
    token, _ = seed_authority(admin, TENANT_A, target, world='real', operation=Operation.EXECUTE, resource_type=ResourceType.ACTION,
                              identity_suffix='-denial-effect')
    with open_core(make_conninfo(pg, user='nexloop_action_worker')) as pool:
        port = object.__new__(effect_execution.EffectExecutionPort)
        port.pool, port.session, port.signer = pool, authenticate_service(pool, token, world='real'), signer
        proof, _ = port._proof(port.session, target)
        payload = canonical_payload({'verb': 'denial-probe'})
        base = {'protocol': effect_execution.PROTOCOL, 'key_id': signer.key_id, **proof, 'parameters_digest': hashlib.sha256(payload.encode()).hexdigest()}
        cases = {'key_unknown': ({**base, 'key_id': 'synthetic-unknown-key'}, signer.material), 'protocol': ({**base, 'protocol': 'nexloop-effect-execution-v0'}, signer.material),
                 'expired': ({**base, 'expires_at': iso(-1)}, signer.material), 'too_far_ahead': ({**base, 'expires_at': iso(40)}, signer.material),
                 'digest': ({**base, 'parameters_digest': '0' * 64}, signer.material), 'signature': (base, secrets.token_bytes(32))}
        for cause, (claims, material) in cases.items():
            text, signature = signed(material, effect_execution.PROTOCOL, claims)
            with psycopg.connect(make_conninfo(pg, user='nexloop_action_worker'), autocommit=True) as db:
                diag = refused(lambda: db.execute('select authz.nexloop_effect_execution_command(%s,%s,%s,%s,%s)',
                                                  (port.session.token_digest, 'real', text, signature, payload)))
            assert (diag.message_primary, diag.message_detail) == ('effect provider authority unavailable', cause), (cause, diag.message_detail)
        # Positive control: the unaltered proof passes the signature gate (it then fails at the probe's unknown verb, not here).
        text, signature = signed(signer.material, effect_execution.PROTOCOL, base)
        with psycopg.connect(make_conninfo(pg, user='nexloop_action_worker'), autocommit=True) as db, pytest.raises(psycopg.Error) as later:
            db.execute('select authz.nexloop_effect_execution_command(%s,%s,%s,%s,%s)', (port.session.token_digest, 'real', text, signature, payload))
        assert later.value.diag.message_detail not in CAUSES


def test_runtime_activation_denials_name_their_cause(signer, admin, pg):
    target = 'eios:action:NexLoop.queue.operations:1'  # the activation's resource is its queue's Action (0037)
    token, _ = seed_authority(admin, TENANT_A, target, world='real', operation=Operation.EXECUTE, resource_type=ResourceType.ACTION,
                              identity_suffix='-denial-activation')
    digest = hashlib.sha256(token.encode()).hexdigest()
    payload = canonical_payload({'queue': 'operations', 'verb': 'create'})
    base = {'protocol': 'nexloop-runtime-activation-v1', 'key_id': signer.key_id, 'resource_id': target, 'expires_at': iso(10),
            'parameters_digest': hashlib.sha256(payload.encode()).hexdigest()}
    cases = {'key_unknown': {**base, 'key_id': 'synthetic-unknown-key'}, 'protocol': {**base, 'protocol': 'nexloop-runtime-activation-v0'},
             'resource': {**base, 'resource_id': 'eios:action:other:1'}, 'expired': {**base, 'expires_at': iso(-1)},
             'too_far_ahead': {**base, 'expires_at': iso(40)}, 'digest': {**base, 'parameters_digest': '0' * 64}}
    for cause, claims in cases.items():
        text, signature = signed(signer.material, 'nexloop-runtime-activation-v1', claims)
        with psycopg.connect(make_conninfo(pg, user='nexloop_domain_worker'), autocommit=True) as db:
            diag = refused(lambda: db.execute('select authz.nexloop_runtime_activation_command(%s,%s,%s,%s,%s)', (digest, 'real', text, signature, payload)))
        assert (diag.message_primary, diag.message_detail) == ('activation denied', cause), (cause, diag.message_primary, diag.message_detail)
    text, _ = signed(signer.material, 'nexloop-runtime-activation-v1', base)
    with psycopg.connect(make_conninfo(pg, user='nexloop_domain_worker'), autocommit=True) as db:
        diag = refused(lambda: db.execute('select authz.nexloop_runtime_activation_command(%s,%s,%s,%s,%s)', (digest, 'real', text, '0' * 64, payload)))
    assert (diag.message_primary, diag.message_detail) == ('activation denied', 'signature')


def test_context_v6_denials_name_their_cause_at_both_gates(signer, admin, pg):
    token, _ = seed_authority(admin, TENANT_A, 'eios:action:nexloop.context.assemble:1', world='real', operation=Operation.EXECUTE,
                              resource_type=ResourceType.ACTION, identity_suffix='-denial-context')
    digest = hashlib.sha256(token.encode()).hexdigest()
    payload = '{}'
    base = {'protocol': 'nexloop-context-v6-v1', 'resource_id': 'eios:action:nexloop.context.assemble:1', 'key_id': signer.key_id,
            'parameters_digest': hashlib.sha256(payload.encode()).hexdigest(), 'expires_at': iso(10)}
    cases = {'protocol': ({**base, 'protocol': 'nexloop-context-v6-v0'}, signer.material), 'resource': ({**base, 'resource_id': 'eios:action:other:1'}, signer.material),
             'expired': ({**base, 'expires_at': iso(-1)}, signer.material), 'too_far_ahead': ({**base, 'expires_at': iso(40)}, signer.material),
             'digest': ({**base, 'parameters_digest': '0' * 64}, signer.material),
             'key_unknown': ({**base, 'key_id': 'synthetic-unknown-key'}, signer.material), 'signature': (base, secrets.token_bytes(32))}
    for cause, (claims, material) in cases.items():
        text, signature = signed(material, 'nexloop-context-v6-v1', claims)
        with psycopg.connect(make_conninfo(pg, user='nexloop_api'), autocommit=True) as db:
            diag = refused(lambda: db.execute('select authz.nexloop_context_v6_command(%s,%s,%s,%s,%s)', (digest, 'real', text, signature, payload)))
        assert (diag.message_primary, diag.message_detail) == ('context v6 unavailable', cause), (cause, diag.message_detail)
    # A caller or world condition of the same gate is 'other' (not a signature cause).
    text, signature = signed(signer.material, 'nexloop-context-v6-v1', base)
    with psycopg.connect(make_conninfo(pg, user='nexloop_api'), autocommit=True) as db:
        diag = refused(lambda: db.execute('select authz.nexloop_context_v6_command(%s,%s,%s,%s,%s)', (digest, 'test', text, signature, payload)))
    assert (diag.message_primary, diag.message_detail) == ('context v6 unavailable', 'other')
    # Positive control: the unaltered proof passes both gates and fails later (assemble authority), with no cause.
    with psycopg.connect(make_conninfo(pg, user='nexloop_api'), autocommit=True) as db, pytest.raises(psycopg.Error) as later:
        db.execute('select authz.nexloop_context_v6_command(%s,%s,%s,%s,%s)', (digest, 'real', text, signature, payload))
    assert later.value.diag.message_primary != 'context v6 unavailable' or later.value.diag.message_detail is None
    assert json.loads(text)['key_id'] == signer.key_id
