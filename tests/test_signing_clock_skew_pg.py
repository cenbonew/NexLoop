"""Hotfix (stage 2026-10-20 21:31:53): a signed command's expires_at is the client's now + SIGNED_COMMAND_TTL (20 s), while the SQL
entries accept up to the database's clock_timestamp() + 25 s. A client clock up to 5 s ahead of the database is accepted; beyond that
the proof is still refused (the SQL bound is unchanged). The client clock is moved by patching the signing module's datetime.

Real PostgreSQL: the durable queue (authz.nexloop_queue_command) end to end, and the effect execution proof
(authz.nexloop_effect_execution_command's signature gate). Every other signing path is covered by the source check below: it reads
SIGNED_COMMAND_TTL and no 25 s signing bound is left."""
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
from pathlib import Path
import re
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
from nexloop_eios.signing import SIGNED_COMMAND_TTL, SQL_SIGNED_COMMAND_BOUND
from support.tenants import TENANT_A

ROOT = Path(__file__).resolve().parents[1]
QUEUE_TARGET = 'eios:action:NexLoop.queue.operations:1'


def ahead(seconds):
    """A datetime whose now() is the real clock plus `seconds` (a client host ahead of the database host)."""
    class Ahead(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=seconds)
    return Ahead


def test_the_margin_is_five_seconds():
    assert SIGNED_COMMAND_TTL == timedelta(seconds=20) and SQL_SIGNED_COMMAND_BOUND - SIGNED_COMMAND_TTL == timedelta(seconds=5)


@pytest.fixture
def signer(admin):
    bootstrap(admin)
    s = AuthoritySigner('synthetic-skew', secrets.token_bytes(32))
    admin.execute('insert into authz.nexloop_authority_signing_keys values(%s,%s,true)', (s.key_id, s.material))
    return s


def test_the_queue_accepts_a_client_three_seconds_ahead_and_refuses_six(signer, admin, pg, monkeypatch):
    with ExitStack() as stack:
        token, _ = seed_authority(admin, TENANT_A, QUEUE_TARGET, world='real', operation=Operation.EXECUTE, resource_type=ResourceType.ACTION,
                                  identity_suffix='-skew-api')
        pool = stack.enter_context(open_core(make_conninfo(pg, user='nexloop_api')))
        queue = PostgresDurableQueue(pool, authenticate_service(pool, token, world='real'), signer, queue='operations')
        monkeypatch.setattr(durable_queue, 'datetime', ahead(3))
        assert queue.accept(source_id='skew', event_id='ahead-3', payload={'text': 'synthetic'})['accepted'] is True
        # Positive control of the bound: 6 s ahead exceeds the database's +25 s window; the SQL signature gate refuses it.
        monkeypatch.setattr(durable_queue, 'datetime', ahead(6))
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='queue signature denied'):
            queue.accept(source_id='skew', event_id='ahead-6', payload={'text': 'synthetic'})
        monkeypatch.setattr(durable_queue, 'datetime', ahead(0))
        assert queue.accept(source_id='skew', event_id='in-sync', payload={'text': 'synthetic'})['accepted'] is True
    assert admin.execute("select count(*) from runtime.nexloop_inbox where source_id='skew'").fetchone()[0] == 2


def effect_signature_gate(admin, pg, signer, monkeypatch, seconds):
    """Build the effect execution proof as EffectExecutionPort does (its _proof, the client clock `seconds` ahead), sign it and send it
    to the SQL entry; return the refusal message (the signature gate answers 'effect provider authority unavailable')."""
    target = 'eios:action:nexloop.service.request:1'
    token, _ = seed_authority(admin, TENANT_A, target, world='real', operation=Operation.EXECUTE, resource_type=ResourceType.ACTION,
                              identity_suffix='-skew-effect-' + str(seconds))
    with open_core(make_conninfo(pg, user='nexloop_action_worker')) as pool:
        port = object.__new__(effect_execution.EffectExecutionPort)
        port.pool, port.session, port.signer = pool, authenticate_service(pool, token, world='real'), signer
        monkeypatch.setattr(effect_execution, 'datetime', ahead(seconds))
        proof, _ = port._proof(port.session, target)
        monkeypatch.setattr(effect_execution, 'datetime', datetime)
        payload = canonical_payload({'verb': 'skew-probe'})
        claims = {'protocol': effect_execution.PROTOCOL, 'key_id': signer.key_id, **proof, 'parameters_digest': hashlib.sha256(payload.encode()).hexdigest()}
        text = canonical_payload(claims)
        signature = hmac.new(signer.material, (effect_execution.PROTOCOL + ':' + text).encode(), 'sha256').hexdigest()
        with psycopg.connect(make_conninfo(pg, user='nexloop_action_worker'), autocommit=True) as db, pytest.raises(psycopg.Error) as refused:
            db.execute('select authz.nexloop_effect_execution_command(%s,%s,%s,%s,%s)', (port.session.token_digest, 'real', text, signature, payload))
        return refused.value.diag.message_primary


def test_the_effect_execution_proof_passes_the_signature_gate_three_seconds_ahead_and_not_six(signer, admin, pg, monkeypatch):
    # 3 s ahead: past the signature gate (the request then fails later, at its unknown verb or profile - not at the signature).
    assert effect_signature_gate(admin, pg, signer, monkeypatch, 3) != 'effect provider authority unavailable'
    # Positive control: 6 s ahead is refused at the signature gate itself.
    assert effect_signature_gate(admin, pg, signer, monkeypatch, 6) == 'effect provider authority unavailable'


SIGNING_MODULES = sorted({p.relative_to(ROOT).as_posix() for p in (ROOT / 'packages/eios-core/src/nexloop_eios').rglob('*.py')
                          if 'SIGNED_COMMAND_TTL' in p.read_text() and p.name != 'signing.py'})


def test_every_signing_path_uses_the_ttl():
    assert len(SIGNING_MODULES) >= 30, SIGNING_MODULES


@pytest.mark.parametrize('module', SIGNING_MODULES)
def test_no_signing_path_keeps_a_25_second_bound(module):
    """Every module that signs with SIGNED_COMMAND_TTL keeps no 25 s signing bound; a remaining timedelta(seconds=25) must be a lease."""
    for line in (ROOT / module).read_text().splitlines():
        if 'timedelta(seconds=25)' in line:
            assert re.search(r'lease_expires_at=now\+timedelta\(seconds=25\)', line), (module, line)
