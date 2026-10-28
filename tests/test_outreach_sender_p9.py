"""Release 53 P9 (scripts/deploy/outreach_sender_p9.py): the outreach sender as the stage Compose file renders it uses the loopback
provider only and mounts no channel credential; each forbidden change is reported by name (no value)."""
import copy
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / 'deploy/stage'
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import outreach_sender_p9 as P  # noqa: E402
from test_stage_compose import interpolate  # noqa: E402


def rendered():
    """docker compose config --format json when docker is here (the form the run sheet pipes), else the same interpolation in Python."""
    docker = shutil.which('docker')
    if docker:
        run = subprocess.run([docker, 'compose', '-f', 'compose.app.example.yaml', '--env-file', 'compose.app.env.example', 'config', '--format', 'json'],
                             cwd=STAGE, capture_output=True, text=True, timeout=60, env={**os.environ, 'REAL_DISPATCH_ENABLED': 'false'})
        assert run.returncode == 0, run.stderr[-2000:]
        return json.loads(run.stdout)
    return yaml.safe_load(interpolate((STAGE / 'compose.app.example.yaml').read_text(), STAGE / 'compose.app.env.example', {}))


def check(config):
    out = io.StringIO()
    code = P.main(io.StringIO(json.dumps(config)), out)
    return code, json.loads(out.getvalue())


def test_the_stage_compose_passes():
    code, out = check(rendered())
    assert (code, out) == (0, {'ok': True, 'problems': []})


@pytest.mark.parametrize('change, problem', [
    (lambda s: s['command'].extend(['--provider', 'webchat']), 'command has --provider'),
    (lambda s: s['command'].append('--real-channel-enabled'), 'command has --real-channel-enabled'),
    (lambda s: s['secrets'].append({'source': 'webchat_provider_credential', 'target': 'webchat_provider_credential'}), 'secret webchat_provider_credential not expected'),
    (lambda s: s['secrets'].append('webchat_provider_credential'), 'secret webchat_provider_credential not expected'),
    (lambda s: s['volumes'].append({'type': 'bind', 'source': '/srv/x/secrets/action-worker', 'target': '/run/channel'}), 'volume /run/channel'),
    (lambda s: s.update(read_only=False), 'read_only'),
    (lambda s: s.update(cap_drop=['NET_RAW']), 'cap_drop'),
    (lambda s: s.update(network_mode='host'), 'network_mode'),
    (lambda s: s.setdefault('environment', {}).update(CHANNEL_TOKEN='zz-value-marker'), 'environment: CHANNEL_TOKEN'),
])
def test_each_forbidden_change_is_named(change, problem):
    config = copy.deepcopy(rendered())
    change(config['services']['outreach-sender'])
    code, out = check(config)
    assert code == 1 and out['ok'] is False and problem in out['problems']
    assert 'zz-value-marker' not in json.dumps(out)  # names only, never a value


def test_a_missing_service_or_input_is_not_a_pass():
    config = copy.deepcopy(rendered())
    del config['services']['outreach-sender']
    assert check(config) == (1, {'ok': False, 'problems': ['outreach-sender missing']})
    out = io.StringIO()
    assert P.main(io.StringIO('not json'), out) == 2 and json.loads(out.getvalue())['ok'] is False
    assert P.main(io.StringIO('{}'), io.StringIO()) == 2
