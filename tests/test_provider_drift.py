"""Owner 2026-10-19: the read-only model provider address drift check (doctor --provider-drift), with a stub resolver. Addresses
are built at runtime from documentation ranges; no real DNS query is made."""
import ipaddress
import json
import re
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


AGENT, CLAIM = '192.0.2.32/27', '192.0.2.96/27'
CIDRS = {'agent': AGENT, 'claim': CLAIM}
STAGE = {'agent': (HOSTS[0],), 'claim': HOSTS}  # stage: the Agent Host calls DeepSeek only; the claim namespace also Ark


def nft(**per_cidr):
    lines = [f'    ip saddr {cidr} ip daddr {{ {",".join(sorted(s))} }} tcp dport 443 accept' for cidr, s in per_cidr.items()]
    return 'table inet nexloop_stage {\n  chain forward {\n' + '\n'.join(lines) + '\n  }\n}\n'


def run(allowlist, answers, text=None, claim=None, **kw):
    """allowlist: the agent's (MODEL_PROVIDER_ALLOWLIST); claim: CLAIM_MODEL_PROVIDER_ALLOWLIST (default the same)."""
    return provider_drift.check({'agent': allowlist, 'claim': claim if claim is not None else allowlist}, STAGE, cidrs=CIDRS,
                                resolver=resolver(answers), nft_text=text, **kw)


def test_no_drift_when_each_namespace_has_its_one_rule_with_its_own_providers():
    """Stage today: Ark only in the claim egress rule, the agent rule DeepSeek only."""
    result = run({A, B, C}, {HOSTS[0]: [{A}], HOSTS[1]: [{B, C}]}, nft(**{AGENT: {A}, CLAIM: {A, B, C}}))
    assert result['checks']['nft_rules_complete'] is True
    assert all(result['checks'].values()), result
    assert result['details']['nft_rules']['agent'] == {'cidr': AGENT, 'rules': 1, 'providers': [HOSTS[0]], 'missing': [], 'extra': [], 'outside_allowlist': []}
    assert result['details']['nft_rules']['claim']['missing'] == []


def test_a_namespace_rule_lost_or_short_of_its_providers_is_a_drift_even_when_another_rule_equals_the_allowlist():
    lost = run({A, B}, {HOSTS[0]: [{A}], HOSTS[1]: [{B}]}, nft(**{CLAIM: {A, B}}))  # the agent rule is gone
    assert lost['checks']['nft_rules_complete'] is False and lost['details']['nft_rules']['agent']['rules'] == 0
    short = run({A, B}, {HOSTS[0]: [{A}], HOSTS[1]: [{B}]}, nft(**{AGENT: {A}, CLAIM: {A}}))  # Ark missing from the claim rule
    assert short['checks']['nft_rules_complete'] is False and short['details']['nft_rules']['claim']['missing'] == [B]
    twice = run({A}, {HOSTS[0]: [{A}], HOSTS[1]: [{A}]}, nft(**{AGENT: {A}}) + nft(**{AGENT: {A}, CLAIM: {A}}))
    assert twice['details']['nft_rules']['agent']['rules'] == 2 and twice['checks']['nft_rules_complete'] is False
    outside = run({A}, {HOSTS[0]: [{A}], HOSTS[1]: [{A}]}, nft(**{AGENT: {A, D}, CLAIM: {A}}))
    assert outside['details']['nft_rules']['agent']['outside_allowlist'] == [D] and outside['checks']['nft_rules_complete'] is False


def test_an_address_outside_the_allowlist_is_reported_missing_across_rotating_samples():
    slept = []
    result = run({A, B}, {HOSTS[0]: [{A}, {A}, {C}], HOSTS[1]: [{B}]}, samples=3, interval=1.5, sleep=slept.append)
    assert result['checks'] == {'provider_hosts_resolve': True, 'provider_addresses_allowed': False}
    assert result['details']['missing'] == [C] and result['details']['hosts'][HOSTS[0]] == {'resolved': [A, C]}
    assert result['details']['allowlists']['agent']['missing'] == [C] and result['details']['allowlists']['claim']['missing'] == [C]
    assert slept == [1.5, 1.5]  # only between rounds


def test_extra_addresses_are_informational_and_an_unresolved_host_fails():
    result = run({A, B, D}, {HOSTS[0]: [{A}], HOSTS[1]: [set()]})
    assert result['details']['allowlists']['agent']['extra'] == [B, D] and result['details']['unresolved_hosts'] == [HOSTS[1]]
    assert result['checks']['provider_hosts_resolve'] is False and result['checks']['provider_addresses_allowed'] is True
    rotated = run({A, B, C}, {HOSTS[0]: [{A}], HOSTS[1]: [{B}]}, nft(**{AGENT: {A}, CLAIM: {A, B, C}}))
    assert rotated['details']['nft_rules']['claim']['extra'] == [C] and rotated['checks']['nft_rules_complete'] is True


def test_input_is_refused():
    for bad in (dict(namespaces={'other': HOSTS}), dict(namespaces={}), dict(namespaces={'agent': ('not a host',)}), dict(samples=0),
                dict(samples=21), dict(interval=-1), dict(cidrs={'agent': AGENT})):
        with pytest.raises(ValueError):
            provider_drift.check({'agent': {A}, 'claim': {A}}, **{'namespaces': STAGE, 'cidrs': CIDRS, **bad}, resolver=lambda h: {A})
    with pytest.raises(ValueError):
        provider_drift.check({'agent': {A}}, STAGE, cidrs=CIDRS, resolver=lambda h: {A})  # no allowlist for the claim namespace
    with pytest.raises(ValueError):
        provider_drift.parse_allowlist('198.51.100.10,not-an-address')
    assert provider_drift.parse_allowlist(f'{A},{B}') == {A, B} and provider_drift.parse_allowlist('') == set()
    assert provider_drift.parse_namespace('agent=api.deepseek.com') == ('agent', ('api.deepseek.com',))
    for bad in ('agent', 'other=api.deepseek.com', 'claim=', 'claim=bad host'):
        with pytest.raises(ValueError):
            provider_drift.parse_namespace(bad)


def test_doctor_reports_the_drift_from_the_private_env_and_rules(tmp_path, monkeypatch, capsys):
    env = (ROOT / 'deploy/stage/stage.env.example').read_text().replace('MODEL_PROVIDER_ALLOWLIST=203.0.113.5', f'MODEL_PROVIDER_ALLOWLIST={A},{B}')
    values = dict(line.split('=', 1) for line in env.splitlines() if line and not line.startswith('#'))
    stage = tmp_path / 'stage.env';stage.write_text(env);os.chmod(stage, 0o600)
    rules_file = tmp_path / 'app-host.rules.nft'
    rules_file.write_text(nft(**{values['AGENT_EGRESS_CIDR']: {A}, values['CLAIM_EGRESS_CIDR']: {A, B}}))
    real = provider_drift.check
    answers = {'now': {HOSTS[0]: [{A}], HOSTS[1]: [{B}]}}
    # The resolver default is bound at definition time: route the doctor's call through the stub.
    monkeypatch.setattr(provider_drift, 'check', lambda allowlist, namespaces, **kw: real(allowlist, namespaces, resolver=resolver(answers['now']), **kw))
    argv = ['nexloop-doctor', '--provider-drift', '--stage-env', str(stage), '--nft-rules', str(rules_file), '--samples', '1', '--interval', '0']
    monkeypatch.setattr(sys, 'argv', argv)
    assert doctor.main() == 0 and json.loads(capsys.readouterr().out)['foundation_checks_passed'] is True
    answers['now'] = {HOSTS[0]: [{A, C}], HOSTS[1]: [{B}]}
    assert doctor.main() == 1
    out = json.loads(capsys.readouterr().out)
    assert out['details']['missing'] == [C] and out['details']['nft_rules']['agent']['missing'] == [C]
    # A namespace configured to call Ark too: the agent rule lacks its address.
    answers['now'] = {HOSTS[0]: [{A}], HOSTS[1]: [{B}]}
    monkeypatch.setattr(sys, 'argv', argv + ['--namespace', 'agent=api.deepseek.com,ark.cn-beijing.volces.com'])
    assert doctor.main() == 1 and json.loads(capsys.readouterr().out)['details']['nft_rules']['agent']['missing'] == [B]
    monkeypatch.setattr(sys, 'argv', argv[:3] + [str(tmp_path / 'absent.env')])
    assert doctor.main() == 1 and json.loads(capsys.readouterr().out)['checks'] == {'provider_drift_input': False}


def test_the_scheduled_check_only_reports():
    script = (ROOT / 'scripts/deploy/provider_drift_check.sh').read_text()
    assert '--provider-drift' in script and 'logger -t nexloop-provider-drift' in script and ':ro' in script
    code = '\n'.join(l for l in script.splitlines() if not l.lstrip().startswith('#'))
    assert not re.search(r'\bnft\s+(-f|add|delete|flush|insert|replace)|systemctl|render\.py', code)  # never edits or reloads the firewall
    timer = (ROOT / 'deploy/stage/systemd/nexloop-provider-drift.timer.example').read_text()
    assert 'OnCalendar=hourly' in timer


def test_stage_today_with_per_namespace_allowlists_has_nothing_outside_its_allowlist():
    """2026-10-19 stage: DeepSeek A in both rules, the new Ark address B and the old one C only in the claim rule. With one shared
    allowlist (A only) the claim rule's Ark addresses were outside it; with CLAIM_MODEL_PROVIDER_ALLOWLIST they are not, and the
    agent namespace keeps DeepSeek only."""
    rules_text = nft(**{AGENT: {A}, CLAIM: {A, B, C}})
    shared = run({A}, {HOSTS[0]: [{A}], HOSTS[1]: [{B}]}, rules_text)
    assert shared['details']['nft_rules']['claim']['outside_allowlist'] == [B, C] and shared['checks']['provider_addresses_allowed'] is False
    split = run({A}, {HOSTS[0]: [{A}], HOSTS[1]: [{B}]}, rules_text, claim={A, B, C})
    assert all(split['checks'].values()), split
    assert split['details']['nft_rules']['claim']['outside_allowlist'] == [] and split['details']['nft_rules']['claim']['extra'] == [C]
    assert split['details']['nft_rules']['agent'] == {'cidr': AGENT, 'rules': 1, 'providers': [HOSTS[0]], 'missing': [], 'extra': [], 'outside_allowlist': []}
    assert split['details']['allowlists']['agent'] == {'variable': 'MODEL_PROVIDER_ALLOWLIST', 'missing': [], 'extra': []}
    # Ark appearing in the agent rule (a hand edit) is outside the agent's allowlist.
    leaked = run({A}, {HOSTS[0]: [{A}], HOSTS[1]: [{B}]}, nft(**{AGENT: {A, B}, CLAIM: {A, B}}), claim={A, B})
    assert leaked['details']['nft_rules']['agent']['outside_allowlist'] == [B] and leaked['checks']['nft_rules_complete'] is False
    assert provider_drift.namespace_allowlists({'MODEL_PROVIDER_ALLOWLIST': A}) == {'agent': {A}, 'claim': {A}}
    assert provider_drift.namespace_allowlists({'MODEL_PROVIDER_ALLOWLIST': A, 'CLAIM_MODEL_PROVIDER_ALLOWLIST': f'{A},{B}'})['claim'] == {A, B}


def test_the_scheduled_check_uses_the_rules_file_the_load_file_includes(tmp_path):
    """Stage keeps dated rules files (app-host-20261019-ark2.nft) included by config/firewall/app-host.nft; NFT_RULES overrides."""
    import subprocess
    root = tmp_path / 'root';(root / 'config/firewall').mkdir(parents=True)
    dated = root / 'config/firewall/app-host-20261019-ark2.nft';dated.write_text(nft(**{AGENT: {A}}))
    (root / 'config/firewall/app-host.nft').write_text(f'table inet nexloop_stage\ndelete table inet nexloop_stage\ninclude "{dated}"\n')
    stage = tmp_path / 'stage.env';stage.write_text('MODEL_PROVIDER_ALLOWLIST=' + A + '\n')
    bin_dir = tmp_path / 'bin';bin_dir.mkdir()
    (bin_dir / 'docker').write_text('#!/bin/sh\necho "$@" > "$DOCKER_ARGS"\necho \'{"checks":{"x":true}}\'\n');(bin_dir / 'docker').chmod(0o755)
    def run_script(**extra):
        env = {**os.environ, 'PATH': f"{bin_dir}:{os.environ['PATH']}", 'NEXLOOP_ROOT': str(root), 'STAGE_ENV': str(stage),
               'BACKEND_IMAGE': 'registry.invalid/backend@sha256:' + '0' * 64, 'DOCKER_ARGS': str(tmp_path / 'args'), **extra}
        result = subprocess.run(['bash', str(ROOT / 'scripts/deploy/provider_drift_check.sh')], env=env, capture_output=True, text=True, timeout=30)
        return result.returncode, (tmp_path / 'args').read_text() if (tmp_path / 'args').exists() else ''
    code, args = run_script()
    assert code == 0 and f'{dated}:/run/drift/app-host.rules.nft:ro' in args
    other = tmp_path / 'other.nft';other.write_text('')
    code, args = run_script(NFT_RULES=str(other))
    assert code == 0 and f'{other}:/run/drift/app-host.rules.nft:ro' in args
    (root / 'config/firewall/app-host.nft').write_text(f'include "{dated}"\ninclude "{other}"\n')
    assert run_script()[0] == 2  # an ambiguous load file is refused, never guessed
