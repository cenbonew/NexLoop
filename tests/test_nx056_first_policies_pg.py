"""NX-056 A5 (docs/design/unified-approvals.md v5 §3, §10; 0325): the first three policies as trusted configuration, and what they hold.

deploy/configuration/approval-policies.v1.json is checked, applied by the real technical configurator and replayed; a changed policy
under the same version, an older version and any other session are refused. On the NX-029 erasure fixture (real Consumer, owner
through the governed entry): an erasure the owner starts and a hold release wait for approval and change nothing; the owner's
confirmation of a Consumer's own request (consumer.erase_on_request) runs at once, and is refused without such a request.
Synthetic data only.
"""
import copy
import json
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from nexloop_eios.approval_policies import ApprovalPoliciesRejected, apply_policies, load_policies, validate_policies
from commercial_fixture import TENANT, publish_registry_actions
from commitment_fixture import commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from multi_authority_fixture import seed_multi_authority
from nexloop_eios.authorization import authenticate_service
from test_erasure_pg import ACTIONS, KEEPER, act, keeper, request  # noqa: F401

POLICIES = Path(__file__).resolve().parents[1] / 'deploy/configuration/approval-policies.v1.json'
PLAN = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)


def configurator(admin, pg, tmp_path):
    admin.execute('alter role nexloop_configurator login')
    path = tmp_path / 'configurator-dsn'
    path.write_text(make_conninfo(pg, user='nexloop_configurator'))
    path.chmod(0o600)
    return path


def test_the_file_is_the_owners_first_batch_and_bad_manifests_are_refused():
    v1 = load_policies(POLICIES)
    by = {p['capability']: p for p in v1['policies']}
    assert set(by) == {'real_dispatch.set', 'consumer.erase', 'retention.release_hold'}
    assert by['real_dispatch.set']['condition'] == {'field': 'enabled', 'equals': True} and by['consumer.erase']['condition'] is None
    assert all(p['quorum'] == 1 and p['approver_roles'] == ['owner'] and p['expires_after_seconds'] == 86400 for p in v1['policies'])
    for change in (lambda p: p.update(quorum=0), lambda p: p.update(capability='approvals.decide'), lambda p: p.update(approver_roles=['admin']),
                   lambda p: p.update(expires_after_seconds=60), lambda p: p.update(condition={'field': 'enabled', 'op': 'gt'}),
                   lambda p: p.update(extra=1), lambda p: p.update(summary_fields=['Body']),
                   # never a Consumer identifier or free text on every approver's screen (L6 review 7)
                   lambda p: p.update(summary_fields=['consumer_id']), lambda p: p.update(summary_fields=['reason']),
                   lambda p: p.update(summary_fields=['notification_text']), lambda p: p.update(summary_fields=['principal_id'])):
        bad = copy.deepcopy(v1)
        change(bad['policies'][0])
        with pytest.raises(ApprovalPoliciesRejected):
            validate_policies(bad)
    with pytest.raises(ApprovalPoliciesRejected):
        validate_policies({**v1, 'policies': v1['policies'] + [v1['policies'][0]]})  # one entry per capability


@PLAN
def test_the_configurator_applies_replays_and_refuses_a_changed_version(commitments, admin, pg, tmp_path):
    dsn = configurator(admin, pg, tmp_path)
    v1 = load_policies(POLICIES)
    assert apply_policies(v1, database_url_file=dsn) == {'configured': True, 'version': 1, 'appended': 3, 'unchanged': 0}
    assert apply_policies(v1, database_url_file=dsn)['unchanged'] == 3  # a replay
    changed = copy.deepcopy(v1)
    changed['policies'][1]['expires_after_seconds'] = 3600
    with pytest.raises(psycopg.Error, match='changed'):
        apply_policies(changed, database_url_file=dsn)
    changed['policies'][1]['version'] = 2  # a new version of the one policy
    changed['version'] = 2
    assert apply_policies(changed, database_url_file=dsn) == {'configured': True, 'version': 2, 'appended': 1, 'unchanged': 2}
    assert admin.execute("select version,expires_after_seconds,published_by from control.nexloop_approval_policies where capability='consumer.erase' order by version").fetchall() == [
        (1, 86400, 'configuration:approval-policies.v1'), (2, 3600, 'configuration:approval-policies.v2')]
    with pytest.raises(psycopg.Error):
        apply_policies(v1, database_url_file=dsn)  # version 1 again is older than the current 2
    with pytest.raises(psycopg.Error, match='may not name a consumer or free text'), admin.transaction():
        admin.execute("""insert into control.nexloop_approval_policies(capability,version,action_version,quorum,approver_roles,expires_after_seconds,
            summary_fields,published_by) values('consumer.erase',9,1,1,'{owner}',86400,'{consumer_id}','x')""")
    with pytest.raises(psycopg.Error, match='trusted configuration only'):
        admin.execute('select control.nexloop_configure_approval_policies(%s::jsonb)', (json.dumps(v1),))


@pytest.fixture
def held(commitments, admin, pg, tmp_path, identity, uow):
    """The NX-029 erasure fixture with consumer.erase_on_request granted too, and the policies of the file applied."""
    from nexloop_eios.assembly import open_core
    from commercial_fixture import commercial_setup
    c = commitments
    actions = ACTIONS + ('nexloop.consumer.erase_on_request',)
    with open_core(make_conninfo(pg, user='nexloop_api')) as api, commercial_setup(admin, pg, tmp_path, api_pool=api, signer=c['signer']) as env:
        env.connector()
        env.link('cust-1', c['consumer'])
        publish_registry_actions(admin, actions)
        c['extra_human_actions'] = actions
        _, token = seed_multi_authority(admin, env['worker'], KEEPER, identity_suffix='-retention-keeper', tenant=TENANT)
        owner, human = c.owner(identity, uow)
        c['keeper_session'] = lambda: authenticate_service(env['worker'], token, world='real')
        apply_policies(load_policies(POLICIES), database_url_file=configurator(admin, pg, tmp_path))
        c.update(env=env, owner_actions=owner, human=human)
        yield c


def erase_on_request(owner, request_id, **kw):
    return owner.erase_consumer_on_request(action_name='nexloop.consumer.erase_on_request', action_version=1, request_id=request_id, **kw)


@PLAN
def test_an_owner_started_erasure_and_a_hold_release_wait_and_a_consumers_own_request_does_not(held, admin):
    c = held
    owner, consumer = c['owner_actions'], c['consumer']
    waiting = act(owner, 'erase_consumer', 'erase-1', consumer_id=consumer, reason='负责人发起删除')
    assert waiting['status'] == 'waiting_approval' and waiting['approval'] == 'required'
    assert request(admin, consumer) is None  # nothing erased, nothing blocked
    hold = act(owner, 'hold_retention', 'hold-1', scope_kind='item_class', scope_ref='message_text', basis='审计期间暂停清除')  # no policy
    released = act(owner, 'release_retention_hold', 'release-1', hold_id=hold['hold_id'], reason='审计结束')
    assert released['status'] == 'waiting_approval'
    assert admin.execute('select released_at from control.nexloop_retention_holds where hold_id=%s', (hold['hold_id'],)).fetchone() == (None,)
    # Without a Consumer's own request the confirmation is refused; it never stands in for an owner-started erasure.
    with pytest.raises(Exception, match='no_pending_consumer_request'):
        erase_on_request(owner, 'confirm-0', consumer_id=consumer, reason='确认')
    with admin.transaction():
        admin.execute('select runtime.nexloop_consumer_erasure_requested(%s,%s,%s,%s)', (TENANT, 'real', consumer, 'chat-1'))
    confirmed = erase_on_request(owner, 'confirm-1', consumer_id=consumer, reason='负责人确认顾客请求')
    assert confirmed['state'] == 'settling' and 'approval' not in confirmed
    assert request(admin, consumer)[:2] == ('consumer-request:chat-1', 'settling')


def bodies(admin):
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
    return admin.execute('select capability,status,body,request_digest from control.nexloop_approval_requests order by requested_at').fetchall()


@PLAN
def test_a_completed_erasure_pseudonymises_the_consumer_in_every_request_body(held, admin):
    c = held
    owner, consumer = c['owner_actions'], c['consumer']
    act(owner, 'erase_consumer', 'erase-1', consumer_id=consumer, reason='负责人发起删除')  # waits, its body names the Consumer
    [(cap, state, body, digest)] = bodies(admin)
    assert cap == 'consumer.erase' and body['consumer_id'] == consumer
    with admin.transaction():
        admin.execute('select runtime.nexloop_consumer_erasure_requested(%s,%s,%s,%s)', (TENANT, 'real', consumer, 'chat-1'))
    erase_on_request(owner, 'confirm-1', consumer_id=consumer, reason='负责人确认顾客请求')
    for _ in range(4):
        keeper(c).run_once()
    assert request(admin, consumer)[1] == 'completed'
    [(_, after_state, after, after_digest)] = bodies(admin)
    assert after['consumer_id'].startswith('pseudonym:') and consumer not in str(after) and after['reason'] == body['reason']
    assert (after_state, after_digest) == (state, digest)  # only the body changed
    counts = admin.execute("select counts from runtime.nexloop_erasure_tombstones where subject_kind='approval_requests'").fetchone()[0]
    assert counts == {'approval_requests': 1}


@PLAN
def test_a_hold_release_runs_on_its_own_clients_resubmission_after_approval(held, admin):
    from test_nx056_dispatch_recheck_pg import approve_as_second_owner
    c = held
    owner = c['owner_actions']
    hold = act(owner, 'hold_retention', 'hold-1', scope_kind='item_class', scope_ref='message_text', basis='审计期间暂停清除')
    waiting = act(owner, 'release_retention_hold', 'release-1', hold_id=hold['hold_id'], reason='审计结束')
    assert approve_as_second_owner(admin, waiting['approval_id'])['status'] == 'approved'
    released = act(owner, 'release_retention_hold', 'release-2', hold_id=hold['hold_id'], reason='审计结束')  # the same body, a new request id
    assert released['released'] is True and released['approval'] == 'consumed' and released['approval_id'] == waiting['approval_id']
    assert admin.execute('select released_at is not null from control.nexloop_retention_holds where hold_id=%s', (hold['hold_id'],)).fetchone() == (True,)
