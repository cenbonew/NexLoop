"""NX-043 report Action (X3 review 4.3 with X6, dispatcher 2026-10-21): nexloop.experiment.report / read_experiment_report.

One owner-only governed read of both reports: the shadow-world difference report (X3) and the experiment result by maturity window
(X6). The tenant and the actor come from the governed entry, never from the body; the output is aggregates with the minimum-sample
suppression and no Consumer id; windows are whole UTC days. Real PostgreSQL; fixtures of the X3 and X6 tests.
"""
import json
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from support.tenants import TENANT_A, TENANT_B
from test_nx043_experiments_pg import publish, register, registration, strategy
from test_nx043_reports_pg import EXPERIMENT, allocate, consumer, pay
from test_nx043_shadow_pg import decide, inbound, key, sha, switch, x3  # noqa: F401

ROOT = __import__('pathlib').Path(__file__).resolve().parents[1]
TODAY = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
HANDLER = 'control.nexloop_governed_read_experiment_report(text,text,text,text,text,jsonb)'


def read(admin, body, tenant=TENANT_A, world='real', principal='owner-principal'):
    return admin.execute("select control.nexloop_governed_read_experiment_report(%s,%s,%s,'owner-subject',%s,%s)",
                         (tenant, world, principal, 'intent-' + uuid.uuid4().hex[:12], Jsonb({'operation': 'read_experiment_report', 'request_id': uuid.uuid4().hex,
                                                                                              **body}))).fetchone()[0]


def shadow_body(f, since=TODAY, until=None):
    return {'kind': 'shadow', 'world': f['shadow'], 'strategy_id': 'followup.later', 'strategy_version': 1, 'since': since.isoformat(),
            'until': (until or since + timedelta(days=1)).isoformat()}


@pytest.fixture
def both(x3):  # noqa: F811
    f = x3
    admin = f['admin']
    switch(admin, f['shadow'], True)
    f['ids'] = []
    for i in range(35):
        c = sha(f'c-act-{i}')
        conv, msg = inbound(admin, c, reply=i < 31)
        f['ids'] += [c, conv, msg]
        decide(f, key(f['shadow'], msg), 'reply')
    publish(admin, strategy('followup.default', follow_up_delay_hours=24), request='act-control')
    register(admin, registration('real', None, experiment_id=EXPERIMENT, stage='online', primary_metric={'key': 'payment', 'direction': 'increase'}))
    allocate(admin, 'candidate', 32)
    allocate(admin, 'control', 32)
    for i in range(8):
        pay(admin, consumer('candidate', i))
    f['ids'] += [consumer(arm, i) for arm in ('candidate', 'control') for i in range(32)]
    return f


def test_the_owner_reads_both_reports_as_aggregates_with_no_person(both):
    f = both
    admin = f['admin']
    s = read(admin, shadow_body(f))
    assert s['cells'] == [{'shadow': 'reply', 'real': 'reply', 'events': 31, 'consumers': 31}] and s['suppressed_cells'] == 'some'
    e = read(admin, {'kind': 'experiment', 'experiment_id': EXPERIMENT, 'as_of': TODAY.isoformat()})
    assert e['conclusion'] == 'reported' and e['scores']['candidate']['successes'] == 8 and e['scores']['control']['unknown'] == 'none'
    text = json.dumps([s, e])
    assert not any(x in text for x in f['ids'])  # no Consumer, conversation or message id
    assert read(admin, shadow_body(f))['report_digest'] == s['report_digest']  # the frozen report answers again
    # The actor is the governed entry's (audited), never a body field.
    audit = admin.execute("select actor_id,payload->>'kind' from runtime.audit_events where action='nexloop.experiment.report_read' order by created_at").fetchall()
    assert audit[:2] == [('owner-principal', 'shadow'), ('owner-principal', 'experiment')]
    for extra in ({'tenant_id': TENANT_B}, {'actor': 'someone'}, {'by': 'someone'}):
        with pytest.raises(psycopg.errors.InvalidParameterValue, match='experiment report: invalid'), admin.transaction():
            read(admin, {**shadow_body(f), **extra})


def test_only_the_owner_reads_and_only_from_real(both):
    f = both
    admin = f['admin']
    assert admin.execute("select subject_rule from control.nexloop_governed_capabilities where capability='experiment.report'").fetchone()[0] == 'human'
    roles = json.loads((ROOT / 'deploy/authorization/workbench-roles.v1.json').read_text())['roles']
    assert sorted(r for r, grants in roles.items() if 'eios:action:nexloop.experiment.report:1' in grants) == ['owner']
    from nexloop_eios.business_actions import PROFILES
    assert PROFILES['experiment.report'] == 'human_owner'
    for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_metrics', 'nexloop_sim_api', 'nexloop_sim_worker'):
        assert not admin.execute('select has_function_privilege(%s,%s,%s)', (role, HANDLER, 'execute')).fetchone()[0]
        for fn in ('runtime.nexloop_shadow_report_freeze(text,text,text,integer,timestamptz,timestamptz,text)',
                   'runtime.nexloop_experiment_report_freeze(text,text,timestamptz,text)'):
            assert not admin.execute('select has_function_privilege(%s,%s,%s)', (role, fn, 'execute')).fetchone()[0]
    for world in ('test', f['shadow']):
        with pytest.raises(psycopg.errors.InvalidParameterValue, match='owner_world_only'), admin.transaction():
            read(admin, shadow_body(f), world=world)


def test_another_tenant_reads_nothing_of_this_one(both):
    f = both
    admin = f['admin']
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='world_unknown'), admin.transaction():
        read(admin, shadow_body(f), tenant=TENANT_B)  # tenant A's shadow world
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='not_found'), admin.transaction():
        read(admin, {'kind': 'experiment', 'experiment_id': EXPERIMENT, 'as_of': TODAY.isoformat()}, tenant=TENANT_B)


def test_windows_off_the_grid_are_refused(both):
    f = both
    admin = f['admin']
    for body in (shadow_body(f, since=TODAY + timedelta(hours=3)), shadow_body(f, until=TODAY + timedelta(hours=5)),
                 {'kind': 'experiment', 'experiment_id': EXPERIMENT, 'as_of': (TODAY - timedelta(hours=2)).isoformat()}):
        with pytest.raises(psycopg.errors.InvalidParameterValue, match='window_not_on_grid'), admin.transaction():
            read(admin, body)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='experiment report: invalid'), admin.transaction():
        read(admin, {'kind': 'experiment', 'experiment_id': EXPERIMENT, 'as_of': 'yesterday-ish'})
