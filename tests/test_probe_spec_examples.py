"""Run sheet 4.2 (AT-058): the five probe spec examples (deploy/stage/probe/*.spec.example.json) are well formed for
scripts/deploy/probe_isolation.py, carry no address (placeholders only), and keep the two Agent Host negative cases."""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import probe_isolation as P  # noqa: E402

SPECS = sorted((ROOT / 'deploy/stage/probe').glob('*.spec.example.json'))
CLASSES = {'tcp': {'reachable', 'unreachable'}, 'pg': {'authenticated', 'server_refused', 'auth_failed', 'client_refused', 'unreachable'},
           'valkey': {'pong', 'acl_refused', 'server_refused', 'client_refused', 'unreachable'}}


def test_every_example_is_a_valid_spec_with_placeholder_addresses():
    assert {p.name.split('.')[0] for p in SPECS} == {'management', 'data-host', 'app-host', 'agent-host', 'ci-user', 'claim-egress'}
    locations = set()
    for path in SPECS:
        spec = json.loads(path.read_text())
        locations.add(spec['location'])
        codes = [p['code'] for p in spec['probes']]
        assert len(codes) == len(set(codes)), path.name
        for probe in spec['probes']:
            assert probe['kind'] in P.KINDS and probe['target'] in spec['targets'] and probe['expect'] in CLASSES[probe['kind']], (path.name, probe)
        for target in spec['targets'].values():
            assert target['host'] == '127.0.0.1' or re.fullmatch(r'<[A-Z0-9_]+>', target['host']), (path.name, target)
            assert type(target['port']) is int
    assert len(locations) == len(SPECS)


def test_the_agent_host_negative_cases_and_the_positive_relay_path():
    spec = json.loads((ROOT / 'deploy/stage/probe/agent-host.spec.example.json').read_text())
    expect = {p['code']: p['expect'] for p in spec['probes']}
    assert expect['data_pg_from_agent'] == expect['data_valkey_from_agent'] == expect['unlisted_target_from_agent'] == 'unreachable'
    assert expect['model_allowlisted_1'] == expect['guard_via_relay'] == 'reachable'


def test_the_reference_endpoint_expectation_carries_its_change_point():
    """Run sheet 4.2 §6: reachable from the management machine is the known stage deviation until the REFERENCE_ENDPOINT_PORT rule
    of app-host.nft.template is applied; the probe names the change (expect -> unreachable)."""
    spec = json.loads((ROOT / 'deploy/stage/probe/management.spec.example.json').read_text())
    probe = next(p for p in spec['probes'] if p['code'] == 'app_reference_endpoint')
    assert probe['expect'] == 'reachable' and probe['note'].startswith('CHANGE-AFTER-NFT-9643') and 'unreachable' in probe['note']
    assert '__REFERENCE_ENDPOINT_PORT__' in (ROOT / 'deploy/stage/firewall/app-host.nft.template').read_text()


def test_the_claim_egress_spec_covers_its_firewall_rules():
    """G-CLAIM-EGRESS: model 443 and DATA_HOST PostgreSQL reachable; DATA_HOST's other ports, an unlisted address (with its positive
    control) and the API unreachable."""
    spec = json.loads((ROOT / 'deploy/stage/probe/claim-egress.spec.example.json').read_text())
    expect = {p['code']: p['expect'] for p in spec['probes']}
    assert expect == {'model_allowlisted_1': 'reachable', 'model_allowlisted_2': 'reachable', 'data_pg': 'reachable', 'data_valkey': 'unreachable',
                      'data_ssh': 'unreachable', 'unlisted_external': 'unreachable', 'api_on_host': 'unreachable', 'api_in_app_net': 'unreachable'}
    assert 'Positive control' in next(p for p in spec['probes'] if p['code'] == 'unlisted_external')['note']
