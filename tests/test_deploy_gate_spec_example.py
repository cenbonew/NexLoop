"""NX-032 run sheet 3.4 §4.7 (gap G8): deploy/stage/deploy-gate.app.example.json names every input deploy_checks.deploy_gate
requires for the app host and only keys it reads; its secret owners follow the stage Compose secret directories. Filled with the
gate test's files, the gate runs every app check without an input_provided failure."""
import ast
import json
import re
from pathlib import Path

import yaml

from nexloop_eios import deploy_checks

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = json.loads((ROOT / 'deploy/stage/deploy-gate.app.example.json').read_text())


def read_keys():
    """The spec keys deploy_gate reads (spec['…'] and spec.get('…') in its source)."""
    import inspect
    source = inspect.getsource(deploy_checks.deploy_gate)
    keys = set(re.findall(r"spec(?:\.get)?\(\s*'([a-z_]+)'", source)) | set(re.findall(r"spec\['([a-z_]+)'\]", source))
    keys |= {k for k in re.findall(r"load\('([a-z_]+)'\)", source)}
    return keys


def test_the_example_names_every_required_app_input_and_only_keys_the_gate_reads():
    assert set(deploy_checks.REQUIRED_INPUTS['app']) <= set(EXAMPLE)
    assert set(EXAMPLE) <= read_keys(), sorted(set(EXAMPLE) - read_keys())
    assert EXAMPLE['host_role'] == 'app' and set(EXAMPLE['admin_roles']) == set(deploy_checks.ADMIN_ROLES)
    assert EXAMPLE['disabled_roles'] == ['nexloop_owner']  # M9: nexloop_bootstrap stays login (migration identity) until decided
    # Placeholders only: no address, no real path.
    text = json.dumps(EXAMPLE)
    assert not re.search(r'\b\d{1,3}(\.\d{1,3}){3}\b', text) and all(v.startswith('<') for k, v in EXAMPLE.items() if isinstance(v, str) and k != 'host_role')


def test_the_secret_owners_cover_every_stage_secret_directory():
    compose = yaml.safe_load((ROOT / 'deploy/stage/compose.app.example.yaml').read_text())
    dirs = {re.sub(r'^\$\{NEXLOOP_ROOT:\?[^}]*\}/secrets/', '', s['file']).split('/')[0] for s in compose['secrets'].values()}
    assert set(EXAMPLE['secret_uids']) == dirs
    assert EXAMPLE['secret_uids']['agent-host'] == EXAMPLE['secret_uids']['agent-host-model'] == 10020 and EXAMPLE['secret_uids']['gateway'] == 0
    assert {v for k, v in EXAMPLE['secret_uids'].items() if k not in ('agent-host', 'agent-host-model', 'gateway')} == {10001}
