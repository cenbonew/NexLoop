"""Owner 2026-10-19: the read-only model provider address drift check (doctor --provider-drift), with a stub resolver. Addresses
are built at runtime from documentation ranges; no real DNS query is made."""
import ipaddress
import json
import os
import sys
from pathlib import Path

import pytest

from nexloop_eios import doctor, provider_drift

ROOT = Path(__file__).resolve().parents[1]
NET = ipaddress.IPv4Network('198.51.100.0/24')  # TEST-NET-2
A, B, C, D = (str(NET[n]) for n in (10, 11, 12, 13))
HOSTS = ('api.deepseek.com', 'ark.cn-beijing.volces.com')


def resolver(answers):
    """answers: host -> list of address sets, one per round (the last repeats)."""
    calls = {h: 0 for h in answers}

    def resolve(host):
        rounds = answers[host]
        value = rounds[min(calls[host], len(rounds) - 1)]
        calls[host] += 1
        return set(value)
    return resolve


def rules(*address_sets):
    lines = [f'    ip saddr 192.0.2.{96 + 32 * n}/27 ip daddr {{ {",".join(sorted(s))} }} tcp dport 443 accept' for n, s in enumerate(address_sets)]
    return 'table inet nexloop_stage {\n  chain forward {\n' + '\n'.join(lines) + '\n  }\n}\n'


def test_no_drift_when_every_resolved_address_is_allowed_and_the_rules_match():
    result = provider_drift.check({A, B, C}, HOSTS, resolver=resolver({HOSTS[0]: [{A}], HOSTS[1]: [{B, C}]}), nft_text=rules({A, B, C}, {A, B, C}))
    assert all(result['checks'].values()) and result['details']['missing'] == [] and result['details']['extra'] == []
    assert result['details']['nft_rules'] == {'egress_rules': 2, 'stale': {}}


def test_an_address_outside_the_allowlist_is_reported_missing_across_rotating_samples():
    slept = []
    result = provider_drift.check({A, B}, HOSTS, resolver=resolver({HOSTS[0]: [{A}, {A}, {C}], HOSTS[1]: [{B}]}), samples=3, interval=1.5,
                                  sleep=slept.append)
    assert result['checks'] == {'provider_hosts_resolve': True, 'provider_addresses_allowed': False}
    assert result['details']['missing'] == [C] and result['details']['hosts'][HOSTS[0]] == {'resolved': [A, C], 'missing': [C]}
    assert slept == [1.5, 1.5]  # only between rounds


def test_extra_addresses_are_informational_and_an_unresolved_host_fails():
    result = provider_drift.check({A, B, D}, HOSTS, resolver=resolver({HOSTS[0]: [{A}], HOSTS[1]: [set()]}))
    assert result['details']['extra'] == [B, D] and result['details']['unresolved_hosts'] == [HOSTS[1]]
    assert result['checks']['provider_hosts_resolve'] is False and result['checks']['provider_addresses_allowed'] is True


def test_rules_rendered_from_an_older_env_are_stale():
    result = provider_drift.check({A, B, C}, HOSTS, resolver=resolver({HOSTS[0]: [{A}], HOSTS[1]: [{B}]}), nft_text=rules({A, B}, {A, B, C}))
    assert result['checks']['nft_rules_match_allowlist'] is False
    (stale,) = result['details']['nft_rules']['stale'].values()
    assert stale == {'missing': [C], 'extra': []}
    assert provider_drift.check({A}, HOSTS, resolver=resolver({HOSTS[0]: [{A}], HOSTS[1]: [{A}]}), nft_text='')['checks']['nft_rules_match_allowlist'] is False


def test_input_is_refused():
    for bad in (dict(hosts=('not a host',)), dict(hosts=()), dict(samples=0), dict(samples=21), dict(interval=-1)):
        with pytest.raises(ValueError):
            provider_drift.check({A}, **{'hosts': HOSTS, **bad}, resolver=lambda h: {A})
    with pytest.raises(ValueError):
        provider_drift.parse_allowlist('198.51.100.10,not-an-address')
    assert provider_drift.parse_allowlist(f'{A},{B}') == {A, B} and provider_drift.parse_allowlist('') == set()


def test_doctor_reports_the_drift_from_the_private_env_and_rules(tmp_path, monkeypatch, capsys):
    env = (ROOT / 'deploy/stage/stage.env.example').read_text().replace('MODEL_PROVIDER_ALLOWLIST=203.0.113.5', f'MODEL_PROVIDER_ALLOWLIST={A},{B}')
    stage = tmp_path / 'stage.env';stage.write_text(env);os.chmod(stage, 0o600)
    nft = tmp_path / 'app-host.rules.nft';nft.write_text(rules({A, B}, {A, B}))
    monkeypatch.setattr(provider_drift, 'system_resolver', resolver({HOSTS[0]: [{A}], HOSTS[1]: [{B}]}))
    argv = ['nexloop-doctor', '--provider-drift', '--stage-env', str(stage), '--nft-rules', str(nft), '--samples', '1', '--interval', '0']
    monkeypatch.setattr(sys, 'argv', argv)
    # The resolver default is bound at definition time: route the doctor's call through the stub.
    real = provider_drift.check
    monkeypatch.setattr(provider_drift, 'check', lambda allowlist, hosts, **kw: real(allowlist, hosts, resolver=provider_drift.system_resolver, **kw))
    assert doctor.main() == 0 and json.loads(capsys.readouterr().out)['foundation_checks_passed'] is True
    monkeypatch.setattr(provider_drift, 'system_resolver', resolver({HOSTS[0]: [{A, C}], HOSTS[1]: [{B}]}))
    assert doctor.main() == 1
    out = json.loads(capsys.readouterr().out)
    assert out['details']['missing'] == [C] and out['checks']['provider_addresses_allowed'] is False
    monkeypatch.setattr(sys, 'argv', argv[:3] + [str(tmp_path / 'absent.env')])
    assert doctor.main() == 1 and json.loads(capsys.readouterr().out)['checks'] == {'provider_drift_input': False}


def test_the_scheduled_check_only_reports():
    script = (ROOT / 'scripts/deploy/provider_drift_check.sh').read_text()
    assert '--provider-drift' in script and 'logger -t nexloop-provider-drift' in script and ':ro' in script
    assert 'nft ' not in script.replace('app-host.rules.nft', '') and 'render.py' not in script  # never edits or reloads the firewall
    timer = (ROOT / 'deploy/stage/systemd/nexloop-provider-drift.timer.example').read_text()
    assert 'OnCalendar=hourly' in timer
