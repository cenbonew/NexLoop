"""NX-041 S7b: the Context of a Run in a simulation world (contract nexloop.context-pack.sim.v1).

A simulation world has its own assembly entry, authz.nexloop_sim_context_command, reading the synthetic Consumer's generated profile
and persona (S3) and the twin's port events (S7) only; the real assembly entries are not changed. Real PostgreSQL on a clean catalog;
fixtures from the S7 tests (worlds by the owner's handler, synthetic Consumers by the generator, the port as nexloop_twin_port).
"""
import json
import re
import uuid
from pathlib import Path

import psycopg
import pytest
from jsonschema import Draft202012Validator, FormatChecker
from psycopg.types.json import Jsonb

from nexloop_eios import consumer_port as P
from support.tenants import TENANT_A
from test_nx041_s7_pg import consumers, digest, port, s7, s7_raw  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
SIM_PACK = Draft202012Validator(json.loads((ROOT / 'packages/contracts/context-pack-sim.schema.json').read_text()), format_checker=FormatChecker())
MARK = 'zz-real-context-marker'
REAL_ENTRIES = ('nexloop_context_v6_command', 'nexloop_role_context_v6_command', 'nexloop_context_v6_fallback_command', 'nexloop_context_manifest_read',
                'nexloop_relationship_context', 'nexloop_context_consumer_conversations')


def assemble(f, consumer, limit=20, world=None, token=None, role='api'):
    with f[role]() as db:
        return db.execute('select authz.nexloop_sim_context_command(%s,%s,%s,%s)',
                          (digest(token or f['token_a']), world or f['a'], consumer, limit)).fetchone()[0]


def seed_real_marks(admin, consumer):
    """Real data carrying a marker, under the same Consumer id as the synthetic one: a Consumer object, a conversation and a message."""
    conversation = uuid.uuid4().hex * 2
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute('''insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
            values(%s,'real','Consumer',%s,1,%s,'test','synthetic',now(),now())''', (TENANT_A, consumer, Jsonb({'name': MARK, 'segment': MARK})))
        admin.execute('''insert into runtime.nexloop_conversations(tenant_id,world,conversation_id,consumer_id,principal_id,idempotency_key)
            values(%s,'real',%s,%s,'customer',%s)''', (TENANT_A, conversation, consumer, uuid.uuid4().hex))
        admin.execute('''insert into runtime.nexloop_conversation_messages(tenant_id,world,conversation_id,sequence,message_id,idempotency_key,payload_digest,record)
            values(%s,'real',%s,1,%s,%s,%s,%s)''', (TENANT_A, conversation, uuid.uuid4().hex * 2, uuid.uuid4().hex, 'a' * 64,
                                                       Jsonb({'body': MARK + ' 我想预约', 'speaker': 'consumer'})))
        # The same Consumer in the shared persona table, in real (review 1.3: behaviour first; 1.1: the world condition).
        admin.execute('''insert into control.nexloop_consumer_personas(tenant_id,world,consumer_id,persona,revision,updated_by)
            values(%s,'real',%s,'real',7,'zz-real-persona-marker')''', (TENANT_A, consumer))


def test_a_sim_run_gets_its_context_from_simulation_sources_only(s7_raw):
    f = s7_raw
    c, other = consumers(f['admin'], f['a'])[:2]
    seed_real_marks(f['admin'], c)
    twin = P.TwinStub(seed='ctx-1')
    events = [port(f).act(twin.act(c, step)) for step in range(1, 5)]
    port(f).act(twin.act(other, 1))
    pack = assemble(f, c, limit=3)
    SIM_PACK.validate(pack)
    assert (pack['world'], pack['consumer_id'], pack['data_mode'], pack['tenant_id']) == (f['a'], c, 'simulation', TENANT_A)
    # Consumer state: the generated profile and the synthetic persona, simulated.
    profile = f['admin'].execute("select properties from runtime.nexloop_sim_objects where world=%s and type_name='Consumer' and object_id=%s", (f['a'], c)).fetchone()[0]
    assert [(s['subsection'], s['content'], s['source']) for s in pack['consumer_state']] == [('profile', profile, 'simulated'),
                                                                                               ('persona', {'persona': 'synthetic'}, 'simulated')]
    # Evidence: this Consumer's newest three port events, oldest first; the newest is the current event and the latest turn.
    assert [e['ref'] for e in pack['evidence']] == ['sim:event:' + e['frozen']['event_id'] for e in events[1:]]
    assert all(e['source'] == 'simulated' and e['evidence_kind'] == 'simulated_input' for e in pack['evidence'])
    assert pack['evidence'][-1]['tags'][:1] == ['latest_consumer_turn'] and all('latest_consumer_turn' not in e['tags'] for e in pack['evidence'][:-1])
    assert pack['current_event']['event_id'] == events[-1]['frozen']['event_id'] and pack['current_event']['source'] == 'simulated'
    assert pack['budget_report'] == {'event_limit': 3, 'events_total': 4, 'events_included': 3, 'omitted': 1}
    assert pack['insufficient'] == [{'code': 'older_events_omitted', 'section': 'evidence'}]
    # Never real: the marker carried by the real Consumer object, conversation and message under the same id is absent; the real
    # persona row of the same Consumer (persona real, revision 7) is not the one read.
    text = json.dumps(pack, ensure_ascii=False)
    assert MARK not in text and 'zz-real-persona-marker' not in text
    assert pack['consumer_state'][1]['content'] == {'persona': 'synthetic'} and pack['consumer_state'][1]['revision'] != '7'
    # Every content_hash is the sha256 of the content given in the item (review 3.3), recomputed as PostgreSQL jsonb text.
    for item in pack['evidence'] + pack['consumer_state']:
        assert f['admin'].execute("select encode(sha256(convert_to(%s::jsonb::text,'UTF8')),'hex')", (json.dumps(item['content'], ensure_ascii=False),)).fetchone()[0] == item['content_hash']
    # The same entry answers again identically (no state changes), and the twin's world time is the server's.
    assert assemble(f, c, limit=3) == pack


def test_a_consumer_without_events_gets_an_explicit_insufficient(s7_raw):
    f = s7_raw
    c = consumers(f['admin'], f['a'])[2]
    f = {**f, 'domain_worker': f['role']('nexloop_domain_worker')}
    pack = assemble(f, c, role='domain_worker')
    SIM_PACK.validate(pack)
    assert pack['current_event'] is None and pack['evidence'] == [] and pack['insufficient'] == [{'code': 'no_simulated_events', 'section': 'current_event'}]


def test_the_sim_entry_refuses_real_test_other_worlds_unknown_consumers_and_other_roles(s7_raw):
    f = s7_raw
    c = consumers(f['admin'], f['a'])[0]
    assert assemble(f, c)['consumer_id'] == c  # positive control
    for world, token in (('real', f['token_real']), ('test', f['token_test']), ('real', f['token_a'])):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='real_world_refused'):
            assemble(f, c, world=world, token=token)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='service authentication denied'):
        assemble(f, c, world=f['b'])  # sim A's credential in world B
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='consumer_unknown'):
        assemble(f, 'e' * 64)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='consumer_unknown'):
        assemble(f, c, world=f['b'], token=f['token_b'])  # a Consumer of world A is unknown in B
    for limit in (0, 257):
        with pytest.raises(psycopg.errors.InvalidParameterValue, match='sim context: invalid'):
            assemble(f, c, limit=limit)
    for role in ('twin', 'stand_in'):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            assemble(f, c, role=role)


SIM_SOURCES = {'runtime.nexloop_sim_objects', 'runtime.nexloop_sim_consumer_events', 'control.nexloop_consumer_personas'}
HELPERS = {'runtime.nexloop_sim_consumer_known': 'runtime.nexloop_sim_consumer_known(text,text,text)',
           'runtime.nexloop_consumer_persona': 'runtime.nexloop_consumer_persona(text,text,text)',
           'authz.nexloop_sim_effect_tenant': 'authz.nexloop_sim_effect_tenant(text,text)',
           'runtime.nexloop_world_now': 'runtime.nexloop_world_now(text,text)',
           'runtime.nexloop_commercial_ts': 'runtime.nexloop_commercial_ts(timestamptz)',
           # Integration: the Host's sim context (after S6c) checks the caller with S6c's role helper.
           'authz.nexloop_app_role': 'authz.nexloop_app_role(text)'}
# What the identity helpers read: the credential tables and the registry (never business data).
IDENTITY = {'authz.nexloop_service_identity', 'control.nexloop_sim_world_status', 'runtime.nexloop_world_kind', 'runtime.nexloop_world_clock',
            'runtime.nexloop_sim_clock_at'}


def names(src):
    return set(re.findall(r'\b(?:ontology|runtime|control|authz)\.(?:nexloop_)?[a-z0-9_]+', src))


def test_the_sim_entry_reads_no_real_source_and_the_real_entries_are_unchanged(s7_raw):
    """Guard (review 1.3), next to the behaviour test above: the sim entry and the helpers it calls (one level) have no dynamic SQL,
    name only simulation sources, identity helpers and each other, never a real conversation, message, Claim, object, memory, recall
    or relationship source; every read of the shared persona table carries the world condition. And no migration from this one on
    redefines a real assembly entry (their helpers were redefined in S1/S5, real/test equivalence checked there; see the golden
    regression test_nx041_s7b_v6_golden_pg)."""
    admin = s7_raw['admin']
    source = lambda sig: admin.execute('select prosrc from pg_proc where oid=%s::regprocedure', (sig,)).fetchone()[0]  # noqa: E731
    entry = source('authz.nexloop_sim_context_command(text,text,text,integer)')
    assert names(entry) <= SIM_SOURCES | set(HELPERS), names(entry) - SIM_SOURCES - set(HELPERS)
    bodies = {'entry': entry} | {name: source(sig) for name, sig in HELPERS.items()}
    for name, body in bodies.items():
        assert not re.search(r'\bexecute\b', body, re.I), name  # no dynamic SQL anywhere on the path
        for forbidden in ('ontology.objects', 'nexloop_conversation', 'nexloop_claims', 'recall', 'memor', 'relationship'):
            assert forbidden not in body, (name, forbidden)
        # No read names the real world (nexloop_consumer_persona's default value 'real' for an unmarked Consumer is not a read).
        assert not re.search(r"world\s*(=|in)\s*\(?\s*'real'", body), name
        assert names(body) <= SIM_SOURCES | set(HELPERS) | IDENTITY | {'authz.nexloop_root_identity'}, (name, names(body) - SIM_SOURCES - set(HELPERS) - IDENTITY)
        # The persona table is shared by every world: each read names the world.
        for m in re.finditer(r'nexloop_consumer_personas', body):
            assert re.search(r'world\s*=\s*p_world', body[m.end():m.end() + 300]), (name, body[m.start():m.end() + 300])
    assert "'real'" not in entry
    migrations = sorted((ROOT / 'packages/eios-core/src/eios/migrations').glob('0*.sql'))
    own = next(m for m in migrations if m.name.endswith('_nx041_sim_context.sql'))
    later = [m for m in migrations if m.name >= own.name]
    redefined = [(m.name, name) for m in later for name in REAL_ENTRIES if re.search(rf'function\s+authz\.{name}\w*\s*\(', m.read_text())]
    assert redefined == []
    # Positive control: the scan finds the real entries where they are defined.
    assert any(re.search(r'function\s+authz\.nexloop_context_v6_command\s*\(', m.read_text()) for m in migrations if m.name < own.name)
