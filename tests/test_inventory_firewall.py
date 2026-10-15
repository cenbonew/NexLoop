"""Stage 2026-10-16 (existing_services_unchanged = changed:firewall): the inventory's firewall comparison ignored only the NexLoop nft
table, but Docker (iptables-nft) writes rules for every Compose network, so NexLoop's own new or re-subnetted networks changed the hash.
Algorithm 2 (scripts/deploy/inventory.py) leaves out the rules naming only a NexLoop network; any other change is still found. Synthetic
rule sets with documentation addresses only."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import inventory as I  # noqa: E402
from nexloop_eios import deploy_checks as D  # noqa: E402

APP_ID, CLAIM_ID, OTHER_ID = 'a' * 64, 'c' * 64, 'e' * 64


def inspected(app_subnet='198.51.100.128/25', claim=True):
    nets = [{'Name': 'nexloop-app_app', 'Id': APP_ID, 'Labels': {'com.docker.compose.project': 'nexloop-app'}, 'IPAM': {'Config': [{'Subnet': app_subnet}]}},
            {'Name': 'other_default', 'Id': OTHER_ID, 'Labels': {'com.docker.compose.project': 'other'}, 'IPAM': {'Config': [{'Subnet': '192.0.2.0/24'}]}}]
    if claim:
        nets.append({'Name': 'nexloop-app_claim_egress', 'Id': CLAIM_ID, 'Labels': {'com.docker.compose.project': 'nexloop-app'},
                     'IPAM': {'Config': [{'Subnet': '203.0.113.96/27'}]}})
    return nets


def ruleset(app_subnet='198.51.100.128/25', claim=True, ufw_extra='', other_extra='', packets=7):
    app_container = app_subnet.split('/')[0].rsplit('.', 1)[0] + '.130'
    lines = ['table ip filter {', '\tchain ufw-user-input {', '\t\ttcp dport 22 counter packets %d bytes 400 accept' % packets]
    if ufw_extra:
        lines.append('\t\t' + ufw_extra)
    lines += ['\t}', '\tchain DOCKER-ISOLATION-STAGE-1 {',
              f'\t\tiifname "br-{APP_ID[:12]}" oifname != "br-{APP_ID[:12]}" counter packets 3 bytes 9 jump DOCKER-ISOLATION-STAGE-2',
              f'\t\tiifname "br-{OTHER_ID[:12]}" oifname != "br-{OTHER_ID[:12]}" counter packets 0 bytes 0 jump DOCKER-ISOLATION-STAGE-2']
    if claim:
        lines.append(f'\t\tiifname "br-{CLAIM_ID[:12]}" oifname != "br-{CLAIM_ID[:12]}" counter packets 0 bytes 0 jump DOCKER-ISOLATION-STAGE-2')
    if other_extra:
        lines.append('\t\t' + other_extra)
    lines += ['\t}', '}', 'table ip nat {', '\tchain POSTROUTING {', f'\t\tip saddr {app_subnet} oifname != "br-{APP_ID[:12]}" counter packets 1 bytes 2 masquerade',
              '\t\tip saddr 192.0.2.0/24 oifname != "br-%s" counter packets 0 bytes 0 masquerade' % OTHER_ID[:12], '\t}', '\tchain DOCKER {',
              f'\t\tiifname != "br-{APP_ID[:12]}" tcp dport 8443 counter packets 5 bytes 6 dnat to {app_container}:8443', '\t}', '}',
              'table inet nexloop_stage {', '\tchain forward {', '\t\tct direction reply accept', '\t}', '}']
    return '\n'.join(lines) + '\n'


def v2(**kw):
    return I.firewall_v2(ruleset(**kw), I.nexloop_networks(inspected(app_subnet=kw.get('app_subnet', '198.51.100.128/25'), claim=kw.get('claim', True))))


def test_only_nexloop_network_changes_keep_the_hash():
    before = v2(app_subnet='198.51.100.0/25', claim=False, packets=1)  # yesterday: another app subnet, no claim_egress, other counters
    after = v2(app_subnet='198.51.100.128/25', claim=True, packets=99)
    assert before['firewall_normalized_sha256'] == after['firewall_normalized_sha256']
    # The old algorithm sees a change for the same two rule sets (the stage finding).
    assert I.firewall_hash(ruleset(app_subnet='198.51.100.0/25', claim=False)) != I.firewall_hash(ruleset())
    kept = '\n'.join(after['firewall_normalized_rules'])
    assert 'br-' + OTHER_ID[:12] in kept and '192.0.2.0/24' in kept and 'tcp dport 22 counter accept' in kept  # other projects and UFW stay
    assert 'br-' + APP_ID[:12] not in kept and 'br-' + CLAIM_ID[:12] not in kept and 'nexloop_stage' not in kept
    assert set(after['nexloop_networks']) == {'nexloop-app_app', 'nexloop-app_claim_egress'}


@pytest.mark.parametrize('change', [{'ufw_extra': 'tcp dport 5432 counter packets 0 bytes 0 accept'},
                                    {'other_extra': 'iifname "eth0" counter packets 0 bytes 0 drop'}])
def test_any_other_rule_change_is_still_found(change):
    before, after = v2(), v2(**change)
    assert before['firewall_normalized_sha256'] != after['firewall_normalized_sha256']
    result = D.inventory_compare({'firewall_algorithm': 2, **before}, {'firewall_algorithm': 2, **after})
    assert result['checks']['existing_services_unchanged'] is False
    assert result['details']['inventory']['problems'] == ['changed:firewall']
    assert result['details']['inventory']['firewall'] == {'lines_only_before': 0, 'lines_only_after': 1}


def test_an_old_before_is_never_a_pass_and_the_owner_may_rebaseline():
    old_before = {'phase': 'before', 'containers': {'other-db': {'image': 'x'}}, 'firewall_without_nexloop_sha256': 'f' * 64}
    after = {'phase': 'after', 'containers': {'other-db': {'image': 'x'}}, 'firewall_without_nexloop_sha256': 'e' * 64, **v2()}
    result = D.inventory_compare(old_before, after)
    assert result['checks']['existing_services_unchanged'] is False
    assert result['details']['inventory']['problems'] == ['firewall_before_old_algorithm']
    assert 'cannot be recomputed' in result['details']['inventory']['firewall']['note']
    rebased = I.rebaseline(old_before, {'phase': 'before', **v2()}, now='2026-10-16T08:00:00+00:00')
    assert rebased['containers'] == old_before['containers'] and rebased['firewall_rebaselined']['previous_sha256'] == 'f' * 64
    result = D.inventory_compare(rebased, after)
    assert result['checks']['existing_services_unchanged'] is True and result['details']['inventory']['firewall'] == {'before_rebaselined': True}
    with pytest.raises(ValueError, match='already'):
        I.rebaseline(rebased, {'phase': 'before', **v2()}, now='x')
    with pytest.raises(ValueError, match='before inventory'):
        I.rebaseline({**old_before, 'phase': 'after'}, {'phase': 'before', **v2()}, now='x')


def test_the_bridge_name_option_and_ipv6_are_honoured():
    nets = I.nexloop_networks([{'Name': 'nexloop-x_n', 'Id': 'b' * 64, 'Labels': {'com.docker.compose.project': 'nexloop-x'},
                                'Options': {'com.docker.network.bridge.name': 'nxbr0'}, 'IPAM': {'Config': [{'Subnet': '2001:db8::/64'}]}}])
    assert nets == {'nexloop-x_n': {'bridge': 'nxbr0', 'subnets': ['2001:db8::/64']}}
    lines = I.normalize_ruleset('table ip filter {\n\tchain c {\n\t\tiifname "nxbr0" accept\n\t\tiifname "nxbr01" accept\n\t}\n}\n', nets)
    assert 'iifname "nxbr0" accept' not in lines and 'iifname "nxbr01" accept' in lines  # whole bridge names only
