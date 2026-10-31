"""I36 (owner 2026-10-31): the owner cancels open commitments with one command (scripts/deploy/commitment_cancel.py) through the
production app, a real workbench login and the governed Commitment.cancel; the operator is refused, a rerun replays, a commitment that
is no longer conditional / open / in_progress is refused with its code. Synthetic data only."""
import io
import json
import secrets
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from nexloop_eios.browser_http import BrowserConfiguration
from nexloop_eios.http_api import ApiConfiguration, create_app
from commitment_fixture import TENANT, commitments, publish_request_actions  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_http import ORIGIN
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_commitments_pg import fresh
from test_real_dispatch_switch_tool import AppTransport, private
from test_workbench_read_pg import CALLER, ROLES, WORKBENCH_APP, add_human, members_file, write_facts

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/deploy'))
import commitment_cancel as T  # noqa: E402

PLAN = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)
REASON = 'I36 负责人 2026-10-31：测试账号对话提取的无期限承诺，统一作废'


@pytest.fixture
def desk(commitments, identity, uow, admin, pg, tmp_path):
    """The workbench with an owner and an operator from workbench-roles (same setup as test_workbench_actions_http)."""
    from nexloop_eios.workbench_roles import apply_members, compile_members
    c = commitments; publish_request_actions(admin)
    def secret(name, content):
        path = tmp_path / name; path.write_bytes(content if isinstance(content, bytes) else content.encode()); path.chmod(0o600); return path
    admin.execute('insert into control.nexloop_browser_applications values(%s,%s,1,true)', (TENANT, WORKBENCH_APP))
    admin.execute('insert into control.nexloop_browser_business_applications values(%s,%s,%s,%s,%s)', (TENANT, WORKBENCH_APP, CALLER, '1', Jsonb(['action.execute'])))
    admin.execute("insert into control.nexloop_browser_rate_policies values(%s,'local_login','nexloop_identity',50,60,true) on conflict do nothing", (TENANT,))
    owner, operator = add_human(admin, 'owner'), add_human(admin, 'operator')
    admin.execute('alter role nexloop_configurator login')
    members = members_file((owner, 'owner'), (operator, 'operator'))
    apply_members(ROLES, members, database_url_file=secret('configurator-dsn', make_conninfo(pg, user='nexloop_configurator')))
    write_facts(admin, compile_members(ROLES, members))
    signer = c['signer']; (tmp_path / 'app-artifacts').mkdir(mode=0o700)
    config = ApiConfiguration(secret('business-dsn', make_conninfo(pg, user='nexloop_api')), secret('authority-key', signer.material),
        tmp_path / 'app-artifacts', signer.key_id, BrowserConfiguration(secret('identity-dsn', identity[0]), secret('rate-key', secrets.token_hex(32)),
        TENANT, 'synthetic-browser-app', ORIGIN), execution_profile='deterministic-test',
        workbench=BrowserConfiguration(tmp_path / 'identity-dsn', tmp_path / 'rate-key', TENANT, WORKBENCH_APP, ORIGIN), mode='test')
    c['app'] = lambda: TestClient(create_app(config), base_url=ORIGIN); c['owner_account'], c['operator_account'] = owner, operator
    return c


def run(c, tmp_path, capsys, who, ids, *extra, ref='i36:test-1'):
    cfg = private(tmp_path, f'cancel-{secrets.token_hex(4)}.json', json.dumps({'schema': T.SCHEMA, 'confirmation_ref': ref, 'reason': REASON, 'commitments': ids}))
    user = private(tmp_path, f'user-{secrets.token_hex(4)}', who['username'] + '\n')
    with c['app']() as client:
        code = T.main(['--origin', ORIGIN, '--password-stdin', '--username-file', str(user), '--config', str(cfg), *extra],
                      stdin=io.StringIO(who['password'] + '\n'), transport=AppTransport(client))
    return code, json.loads(capsys.readouterr().out)


def overview(c):
    from test_workbench_read_pg import workbench_login
    with c['app']() as client:
        assert workbench_login(client, c['owner_account']).status_code == 200
        return client.get('/api/v1/workbench/overview').json()['commitments']


def status(c, commitment):
    return c.commitment(commitment)['properties']['status']


@PLAN
def test_the_owner_cancels_open_and_conditional_commitments_and_a_rerun_replays(desk, tmp_path, capsys):
    c = desk
    open_, _ = fresh(c, due_in=None)  # no due date, as on stage
    conditional, _ = fresh(c, due_in=None, reply='您确认地址后，我们就给您补发。', quote='您确认地址后，我们就给您补发',
                           modality='conditional', condition='您确认地址后', predicate='补发')
    assert (status(c, open_), status(c, conditional)) == ('open', 'conditional')
    before = overview(c)
    assert before['data']['open'] == 2 and before['data']['exceptions'], before  # no active plan: labels on the card
    code, dry = run(c, tmp_path, capsys, c['owner_account'], [open_, conditional], '--dry-run')
    assert code == 0 and dry['commitments'] == [open_, conditional] and status(c, open_) == 'open'
    code, out = run(c, tmp_path, capsys, c['owner_account'], [open_, conditional])
    assert code == 0 and [r['result'] for r in out['commitments']] == ['cancelled', 'cancelled'], out
    text = json.dumps(out, ensure_ascii=False)
    assert c['owner_account']['password'] not in text and REASON not in text and c['owner_account']['username'] not in text
    c.tick()
    assert (status(c, open_), status(c, conditional)) == ('cancelled', 'cancelled')
    after = overview(c)  # the card: nothing open and no labels; the exceptions stay in the history (commitments page, all group)
    assert after['data'] == {'open': 0, 'breached': 0, 'exceptions': []}, after
    code, again = run(c, tmp_path, capsys, c['owner_account'], [open_, conditional])
    assert code == 0 and [r['result'] for r in again['commitments']] == ['replayed', 'replayed'], again
    # A new confirmation reference is a new request: the cancelled commitment is refused in SQL, not replayed.
    code, refused = run(c, tmp_path, capsys, c['owner_account'], [open_], ref='i36:test-2')
    assert code == 1 and refused['commitments'][0]['result'] == 'refused' and refused['commitments'][0]['code'] == 'not_allowed_in_state'


@PLAN
def test_the_operator_is_refused_and_nothing_changes(desk, tmp_path, capsys):
    c = desk; commitment, _ = fresh(c, due_in=None)
    code, out = run(c, tmp_path, capsys, c['operator_account'], [commitment])
    assert code == 1 and out['commitments'][0]['status'] == 403 and out['commitments'][0]['code'] == 'forbidden', out
    c.tick()
    assert status(c, commitment) == 'open'


@PLAN
def test_a_breached_commitment_is_refused(desk, tmp_path, capsys):
    c = desk; commitment, _ = fresh(c, due_in=timedelta(seconds=1))
    time.sleep(1.5); c.tick()
    assert status(c, commitment) == 'breached'
    code, out = run(c, tmp_path, capsys, c['owner_account'], [commitment])
    assert code == 1 and out['commitments'][0]['code'] == 'not_allowed_in_state', out
    assert status(c, commitment) == 'breached'


def test_the_example_is_valid_and_bad_configurations_are_refused():
    example = (Path(__file__).resolve().parents[1] / 'deploy/examples/commitment-cancel.example.json').read_text()
    good = T.load_config(example)
    assert len(good['commitments']) == 2
    for bad in ({**good, 'commitments': []}, {**good, 'commitments': good['commitments'] * 2}, {**good, 'extra': 1},
                {**good, 'commitments': ['A' * 64]}, {**good, 'commitments': ['0' * 63]}, {**good, 'confirmation_ref': ' x'},
                {**good, 'commitments': ['%064x' % i for i in range(501)]}):
        with pytest.raises(T.Refused):
            T.load_config(json.dumps(bad))
