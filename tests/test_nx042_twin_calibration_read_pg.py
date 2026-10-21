"""NX-042 T4 (design §3.2, owner D2/D3): the aggregate calibration read of the consumer twin, for the read-only metrics role.

Real PostgreSQL on a clean catalog; conversations, messages, personas, erasures and contact restrictions seeded by admin as synthetic
operational state with triggers off (replica); the read itself runs as the real nexloop_metrics role.
- D3: a group of 29 Consumers is not output, 30 is; a daypart below 30 suppresses every daypart.
- Excluded and counted: simulated_agent and synthetic personas, erased Consumers; sim:, shadow: and test rows are never read.
- No per-Consumer value: only counts and rates; never a Consumer, conversation or message id or a text.
- Independence: reference and calibration windows by time, and a Consumer of the reference window is not in the calibration window.
- Any role other than nexloop_metrics is refused.
"""
from datetime import UTC, datetime, timedelta
import hashlib
import json
import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A, TENANT_B

SPLIT = datetime(2026, 7, 1, tzinfo=UTC)
MARK = 'zz-calibration-text-marker'


def cid(tag):
    return hashlib.sha256(tag.encode()).hexdigest()


@pytest.fixture
def db(admin, pg):
    bootstrap(admin)
    for t in (TENANT_A, TENANT_B):
        admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (t,))
    return admin, pg


def seed(admin, tag, n, at, *, replied=0, refused=0, tenant=TENANT_A, persona=None, erased=False):
    """n Consumers contacted by an agent at `at`; the first `replied` reply within 24 hours, the first `refused` refuse contact."""
    consumers = []
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        for i in range(n):
            consumer = cid(f'{tag}-{i}')
            conversation = cid(f'conv-{tag}-{i}')
            consumers.append(consumer)
            admin.execute("""insert into runtime.nexloop_conversations(tenant_id,world,conversation_id,consumer_id,principal_id,idempotency_key,last_sequence)
                values(%s,'real',%s,%s,%s,%s,2)""", (tenant, conversation, consumer, 'p-' + consumer[:12], 'k-' + consumer[:12]))
            rows = [(1, {'direction': 'outbound', 'sender_kind': 'agent', 'body': MARK, 'accepted_at': at.isoformat()})]
            if i < replied:
                rows.append((2, {'body': MARK, 'accepted_at': (at + timedelta(hours=3)).isoformat()}))
            for seq, record in rows:
                admin.execute("""insert into runtime.nexloop_conversation_messages(tenant_id,world,conversation_id,sequence,message_id,idempotency_key,payload_digest,record)
                    values(%s,'real',%s,%s,%s,%s,%s,%s)""", (tenant, conversation, seq, cid(f'm-{tag}-{i}-{seq}'), f'k-{seq}', 'd' * 64, Jsonb(record)))
            if i < refused:
                restriction(admin, tenant, 'real', consumer, at + timedelta(hours=5))
            if persona:
                admin.execute("insert into control.nexloop_consumer_personas(tenant_id,world,consumer_id,persona,revision,updated_by) values(%s,'real',%s,%s,1,'seed')",
                              (tenant, consumer, persona))
            if erased:
                admin.execute("""insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark)
                    values(%s,'real',%s,%s,'owner','seed','synthetic','purging',0)""", (tenant, 'erase-' + consumer[:16], consumer))
    return consumers


def restriction(admin, tenant, world, consumer, at):
    admin.execute("""insert into control.nexloop_contact_restrictions(tenant_id,world,consumer_id,active,control_revision,message_id,conversation_id,rule_version,
        rule_id,certainty,matched_text,restricted_at) values(%s,%s,%s,true,1,%s,%s,1,'r','refuse',%s,%s)""", (tenant, world, consumer, 'm', 'c', MARK, at))


def read(pg, window='calibration', split=SPLIT, span=28, tenant=TENANT_A, role='nexloop_metrics'):
    with psycopg.connect(make_conninfo(pg, user=role), autocommit=True) as conn:
        return conn.execute('select authz.nexloop_twin_calibration_aggregates(%s,%s,%s,%s)', (tenant, split, span, window)).fetchone()[0]


def groups(out):
    return {g['group']: g for g in out['groups']}


def test_a_group_of_29_consumers_is_not_output_and_30_is(db):
    admin, pg = db
    at = SPLIT + timedelta(days=3, hours=9)  # daypart 06-12
    seed(admin, 'a', 29, at, replied=10, refused=2)
    out = read(pg)
    assert out['groups'] == [] and out['suppressed'] == ['all', 'daypart'] and out['world'] == 'real' and out['min_group_consumers'] == 30
    seed(admin, 'b', 1, at)
    out = read(pg)
    # All 30 Consumers share one daypart; the other dayparts have none (below 30), so no daypart is output.
    assert out['suppressed'] == ['daypart'] and list(groups(out)) == ['all']
    assert groups(out)['all'] == {'group': 'all', 'consumers': 30, 'replied_24h': 10, 'refused': 2, 'reply_rate_24h': 0.3333, 'refusal_rate': 0.0667}


def test_every_daypart_is_output_only_when_each_has_30(db):
    admin, pg = db
    for d in range(4):
        seed(admin, f'd{d}', 30, SPLIT + timedelta(days=2, hours=6 * d + 1), replied=d * 5)
    out = read(pg)
    g = groups(out)
    assert out['suppressed'] == [] and g['all']['consumers'] == 120 and g['daypart_utc_18_24']['replied_24h'] == 15
    assert [g[f'daypart_utc_{6 * d:02d}_{6 * d + 6:02d}']['consumers'] for d in range(4)] == [30, 30, 30, 30]
    # One daypart with 29: the others would give it away (all minus the rest), so none is output.
    seed(admin, 'x', 1, SPLIT + timedelta(days=2, hours=1))  # all 121, 00-06 at 31; then drop one from 06-12 by an erasure
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("""insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark)
            values(%s,'real','erase-d1',%s,'owner','seed','synthetic','purging',0)""", (TENANT_A, cid('d1-0')))
    out = read(pg)
    assert out['suppressed'] == ['daypart'] and list(groups(out)) == ['all'] and groups(out)['all']['consumers'] == 120
    assert out['excluded']['erasing'] == 1


def test_non_real_personas_erased_consumers_and_sim_test_worlds_are_excluded_and_counted(db):
    admin, pg = db
    at = SPLIT + timedelta(days=5, hours=13)
    real = seed(admin, 'r', 30, at)
    seed(admin, 'agent', 7, at, replied=7, persona='simulated_agent')
    seed(admin, 'syn', 5, at, replied=5, persona='synthetic')
    seed(admin, 'er', 3, at, replied=3, erased=True)
    seed(admin, 'other', 40, at, replied=40, tenant=TENANT_B)  # another tenant
    # Refusals recorded in test, a sim world and a shadow world for real Consumers never count.
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        for world in ('test', 'sim:' + str(uuid.uuid4()), 'shadow:' + str(uuid.uuid4())):
            for c in real:
                restriction(admin, TENANT_A, world, c, at + timedelta(hours=1))
    out = read(pg)
    assert groups(out)['all'] == {'group': 'all', 'consumers': 30, 'replied_24h': 0, 'refused': 0, 'reply_rate_24h': 0.0, 'refusal_rate': 0.0}
    assert out['excluded'] == {'non_real_persona': 12, 'erasing': 3, 'in_reference_window': 0}
    # Only world 'real' is read: the body does not name another world, and conversations are real only by their own CHECK.
    src = admin.execute("select prosrc from pg_proc where oid='runtime.nexloop_twin_calibration_aggregates(text,timestamptz,timestamptz,timestamptz)'::regprocedure").fetchone()[0]
    assert src.count("world='real'") >= 3 and 'p_world' not in src


def test_no_per_consumer_value_id_or_text_is_ever_returned(db):
    admin, pg = db
    consumers = seed(admin, 'p', 31, SPLIT + timedelta(days=1, hours=20), replied=4, refused=1)
    text = json.dumps(read(pg)) + json.dumps(read(pg, 'reference', SPLIT + timedelta(days=56), 28))
    assert MARK not in text and not any(c in text for c in consumers) and not any(c[:12] in text for c in consumers)
    for g in read(pg)['groups']:
        assert set(g) == {'group', 'consumers', 'replied_24h', 'refused', 'reply_rate_24h', 'refusal_rate'} and g['consumers'] >= 30


def test_reference_and_calibration_windows_never_share_a_consumer(db):
    admin, pg = db
    seed(admin, 'old', 30, SPLIT - timedelta(days=10), replied=30)  # reference window only
    both = seed(admin, 'both', 30, SPLIT - timedelta(days=5))  # contacted in both windows
    seed(admin, 'new', 30, SPLIT + timedelta(days=4), replied=6)  # calibration window only
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        for i, c in enumerate(both):
            conv = cid(f'conv-both-{i}')
            admin.execute("""insert into runtime.nexloop_conversation_messages(tenant_id,world,conversation_id,sequence,message_id,idempotency_key,payload_digest,record)
                values(%s,'real',%s,3,%s,'k-3',%s,%s)""", (TENANT_A, conv, cid(f'later-{i}'), 'd' * 64,
                Jsonb({'direction': 'outbound', 'sender_kind': 'agent', 'accepted_at': (SPLIT + timedelta(days=6)).isoformat()})))
    ref, cal = read(pg, 'reference'), read(pg, 'calibration')
    assert groups(ref)['all']['consumers'] == 60 and groups(ref)['all']['replied_24h'] == 30
    assert groups(cal)['all']['consumers'] == 30 and groups(cal)['all']['replied_24h'] == 6 and cal['excluded']['in_reference_window'] == 30


def test_window_parameters_are_checked(db):
    _, pg = db
    for split, span, window in ((SPLIT + timedelta(hours=1), 28, 'calibration'), (SPLIT, 30, 'calibration'), (SPLIT, 28, 'all'),
                                (datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0), 28, 'calibration')):
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            read(pg, window, split, span)
    assert read(pg, 'reference', datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0), 28)['groups'] == []


@pytest.mark.parametrize('role', ['nexloop_api', 'nexloop_domain_worker', 'nexloop_evo_reader', 'nexloop_sim_api', 'nexloop_owner'])
def test_any_role_but_metrics_is_refused(db, role):
    admin, pg = db
    admin.execute(f'alter role {role} login')
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        read(pg, role=role)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # the inner function is executable by its owner only
        with psycopg.connect(make_conninfo(pg, user='nexloop_metrics'), autocommit=True) as conn:
            conn.execute('select runtime.nexloop_twin_calibration_aggregates(%s,%s,%s,null)', (TENANT_A, SPLIT, SPLIT + timedelta(days=28)))
    holders = {r[0] for r in admin.execute("""select r.rolname from pg_roles r where has_function_privilege(r.oid,
        'authz.nexloop_twin_calibration_aggregates(text,timestamptz,integer,text)','execute') and not r.rolsuper""").fetchall()}
    assert holders == {'nexloop_metrics', 'nexloop_owner'}
