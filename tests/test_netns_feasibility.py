"""NX-032 slice 4 pre-check: the namespace feasibility assessment (read-only; probes only inside throwaway namespaces).
The decision logic is exercised with injected kernel settings and probe results; on this machine the real run reports itself."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import netns_feasibility as nf  # noqa: E402

TOOLS = {'unshare': True, 'ip': True, 'slirp4netns': True, 'pasta': False, 'podman': True, 'valkey-server': False, 'docker': False}
OK = {name: {'ok': True, 'exit': 0, 'stderr_tail': ''} for name in nf.PROBES}
OPEN = {'max_user_namespaces': 63000, 'max_net_namespaces': 63000, 'unprivileged_userns_clone': None, 'apparmor_restrict_unprivileged_userns': 0}


def test_linux_with_unprivileged_user_namespaces_is_feasible():
    out = nf.assess(platform='linux', settings=OPEN, tools=TOOLS, probes=OK)
    assert out['verdict'] == 'feasible' and out['reasons'] == [] and out['valkey_for_tls_negatives'] == 'container'


def test_each_restriction_is_named():
    cases = {
        'max_user_namespaces_is_0': ({**OPEN, 'max_user_namespaces': 0}, OK),
        'unprivileged_userns_clone_disabled': ({**OPEN, 'unprivileged_userns_clone': 0}, OK),
        'apparmor_restricts_unprivileged_userns': ({**OPEN, 'apparmor_restrict_unprivileged_userns': 1},
                                                   {**OK, 'userns_netns': {'ok': False, 'exit': 1, 'stderr_tail': 'Operation not permitted'}}),
        'probe_failed:veth_pair': (OPEN, {**OK, 'veth_pair': {'ok': False, 'exit': 2, 'stderr_tail': ''}}),
    }
    for reason, (settings, probes) in cases.items():
        out = nf.assess(platform='linux', settings=settings, tools=TOOLS, probes=probes)
        assert out['verdict'] == 'not_feasible' and reason in out['reasons'], reason
    # AppArmor's restriction only blocks when the probe actually fails (a profile may allow unshare).
    assert nf.assess(platform='linux', settings={**OPEN, 'apparmor_restrict_unprivileged_userns': 1}, tools=TOOLS, probes=OK)['verdict'] == 'feasible'
    missing = nf.assess(platform='linux', settings=OPEN, tools={**TOOLS, 'ip': False}, probes={})
    assert 'tool_missing:ip' in missing['reasons']
    no_user_net = nf.assess(platform='linux', settings=OPEN, tools={**TOOLS, 'slirp4netns': False}, probes=OK)
    assert no_user_net['verdict'] == 'feasible' and any(r.startswith('note:no_user_mode_network') for r in no_user_net['reasons'])


def test_settings_are_read_from_proc(tmp_path):
    (tmp_path / 'user').mkdir()
    (tmp_path / 'kernel').mkdir()
    (tmp_path / 'user' / 'max_user_namespaces').write_text('0\n')
    (tmp_path / 'kernel' / 'apparmor_restrict_unprivileged_userns').write_text('1\n')
    assert nf.read_settings(tmp_path) == {'max_user_namespaces': 0, 'max_net_namespaces': None, 'unprivileged_userns_clone': None,
                                          'apparmor_restrict_unprivileged_userns': 1}


def test_the_real_run_on_this_machine_reports_a_verdict():
    run = subprocess.run([sys.executable, str(ROOT / 'scripts/deploy/netns_feasibility.py')], capture_output=True, text=True, timeout=60)
    out = json.loads(run.stdout)
    assert out['verdict'] in ('feasible', 'not_feasible') and run.returncode == (0 if out['verdict'] == 'feasible' else 1)
    if not sys.platform.startswith('linux'):
        assert out['reasons'] == ['not_linux']
