"""scripts/deploy/data_dirs.py (D-LEDGER): every ${NEXLOOP_ROOT}/data bind directory of the APP_HOST Compose file is prepared with
install -d (owner = the service account, 0700) and checked; a missing, root-owned (Docker-created) or foreign directory fails, and
no directory found fails too (never a silent pass)."""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import data_dirs as D  # noqa: E402

ENV = {'NEXLOOP_ROOT': '/srv/nexloop', 'AGENT_HOST_UID': '10020'}


def dirs():
    return D.bind_dirs(D.COMPOSE, ENV)


def prepared(**over):
    value = {d['path']: {'kind': 'directory', 'uid': d['uid'], 'mode': 0o700} for d in dirs()}
    value.update(over)
    return lambda path: value.get(path)


def test_every_bind_data_directory_is_listed_with_its_account():
    by = {d['path'].rsplit('/data/', 1)[1]: d for d in dirs()}
    assert set(by) == {'runtime', 'artifacts', 'webchat-delivery', 'relay-vault', 'relay-vault-2', 'incentive-ledger', 'outreach-outbox'}
    assert by['runtime']['uid'] == 10020 and by['runtime']['services'] == ['agent-host']
    assert by['incentive-ledger'] == {'path': '/srv/nexloop/data/incentive-ledger', 'uid': 10001, 'services': ['incentive-issuer']}
    assert all(d['uid'] == 10001 for k, d in by.items() if k != 'runtime') and len(by['artifacts']['services']) > 10


def test_plan_prints_one_install_line_per_directory():
    lines = D.plan(dirs())
    assert 'install -d -o 10001 -g 10001 -m 0700 /srv/nexloop/data/incentive-ledger' in lines
    assert 'install -d -o 10001 -g 10001 -m 0700 /srv/nexloop/data/outreach-outbox' in lines
    assert 'install -d -o 10020 -g 10020 -m 0700 /srv/nexloop/data/runtime' in lines and len(lines) == len(set(lines)) == 7


def test_prepared_directories_pass():
    assert D.check(dirs(), prepared()) == {'pass': True, 'checked': 7, 'failing': []}


LEDGER = '/srv/nexloop/data/incentive-ledger'


@pytest.mark.parametrize('state,reason', [
    (None, 'missing'),
    ({'kind': 'directory', 'uid': 0, 'mode': 0o755}, 'owned_by_root'),  # stage 2026-11-02: Docker created it
    ({'kind': 'directory', 'uid': 10020, 'mode': 0o700}, 'wrong_owner'),
    ({'kind': 'directory', 'uid': 10001, 'mode': 0o500}, 'owner_cannot_write'),
    ({'kind': 'symlink', 'uid': 10001, 'mode': 0o777}, 'not_a_directory'),
    ({'kind': 'other', 'uid': 10001, 'mode': 0o600}, 'not_a_directory')])
def test_a_directory_not_prepared_fails_for_its_reason(state, reason):
    out = D.check(dirs(), prepared(**{LEDGER: state}))
    assert out['pass'] is False and [(f['path'], f['reason'], f['services']) for f in out['failing']] == [(LEDGER, reason, ['incentive-issuer'])]


def test_nothing_to_check_fails(tmp_path):
    compose = tmp_path / 'compose.yaml';compose.write_text('services:\n  x:\n    image: y\n')
    assert D.check(D.bind_dirs(compose, ENV), prepared()) == {'pass': False, 'checked': 0, 'failing': [], 'reason': 'no_data_directories'}


def test_unknown_root_or_account_is_an_input_error(tmp_path):
    with pytest.raises(D.InputError, match='NEXLOOP_ROOT'):
        D.bind_dirs(D.COMPOSE, {'AGENT_HOST_UID': '10020'})
    with pytest.raises(D.InputError, match='numeric uid'):
        D.bind_dirs(D.COMPOSE, {'NEXLOOP_ROOT': '/srv/nexloop'})
    compose = tmp_path / 'compose.yaml'
    compose.write_text('services:\n  a:\n    volumes: ["${NEXLOOP_ROOT:?r}/data/x:/x"]\n  b:\n    user: "0:0"\n    volumes: ["${NEXLOOP_ROOT:?r}/data/x:/y"]\n')
    with pytest.raises(D.InputError, match='different accounts'):
        D.bind_dirs(compose, ENV)


def test_live_check_on_real_directories(tmp_path, capsys):
    """The CLI on this host's files: a directory owned by the test user passes; one missing fails, exit 1."""
    root = tmp_path / 'root';(root / 'data').mkdir(parents=True)
    compose = tmp_path / 'compose.yaml'
    uid = os.getuid()
    compose.write_text(f'services:\n  a:\n    user: "{uid}:{uid}"\n    volumes: ["${{NEXLOOP_ROOT:?r}}/data/ledger:/l"]\n')
    env = tmp_path / 'stage.env';env.write_text(f'NEXLOOP_ROOT={root}\n')
    args = ['--stage-env', str(env), '--compose', str(compose)]
    assert D.main(['check', *args]) == 1 and json.loads(capsys.readouterr().out)['failing'][0]['reason'] == 'missing'
    assert D.main(['plan', *args]) == 0 and capsys.readouterr().out.strip() == f'install -d -o {uid} -g {uid} -m 0700 {root}/data/ledger'
    (root / 'data/ledger').mkdir(mode=0o700)
    assert D.main(['check', *args]) == 0 and json.loads(capsys.readouterr().out)['pass'] is True
    (root / 'data/ledger').chmod(0o500)
    assert D.main(['check', *args]) == 1 and json.loads(capsys.readouterr().out)['failing'][0]['reason'] == 'owner_cannot_write'
    (root / 'data/ledger').chmod(0o700)
