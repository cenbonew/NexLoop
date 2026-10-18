"""Model provider address drift (owner 2026-10-19; v0.1 known risk "the model provider egress is a static IP allowlist").

The model egress rules accept numeric addresses only (MODEL_PROVIDER_ALLOWLIST in the private stage env, rendered into the
agent and claim egress rules of app-host.rules.nft). A provider host that starts resolving to an address outside that list
fails every connection by timeout. This check, read only, resolves each model endpoint host (IPv4, several samples: CDN answers
rotate) and compares:
- missing: an address a host resolves to that the allowlist lacks (connections to it time out) -> the check fails;
- extra: an allowlisted address no host resolved to in this run (informational: it may be an earlier answer of a rotating CDN);
- the rendered rules: each egress rule that names the allowlist (agent and claim egress) carries exactly the env's addresses
  (a rule rendered from an older env is a drift too).
It never changes the firewall: the owner confirms and the rules are re-rendered and reloaded as before (OPERATIONS 8a)."""
import ipaddress
import re
import socket
import time

DEFAULT_HOSTS = ('api.deepseek.com', 'ark.cn-beijing.volces.com')
_HOST = re.compile(r'(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}')
_RULE = re.compile(r'ip saddr (\S+) ip daddr \{([^}]*)\} tcp dport 443 accept')


def system_resolver(host):
    """The IPv4 addresses the system resolver gives for a host now (empty when it does not resolve)."""
    try:
        return {info[4][0] for info in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)}
    except OSError:
        return set()


def parse_allowlist(value):
    addresses = {part.strip() for part in (value or '').split(',') if part.strip()}
    for address in addresses:
        ipaddress.IPv4Address(address)
    return addresses


def rule_sets(nft_text):
    """{source CIDR: set of addresses} of every `ip saddr X ip daddr { ... } tcp dport 443 accept` rule (the model egress rules)."""
    out = {}
    for source, body in _RULE.findall(nft_text or ''):
        out[source] = {a.strip() for a in body.split(',') if a.strip()}
    return out


def check(allowlist, hosts=DEFAULT_HOSTS, *, resolver=system_resolver, samples=1, interval=0.0, sleep=time.sleep, nft_text=None):
    """{checks, details} for doctor. allowlist: the env's addresses (set); nft_text: the rendered rules (optional)."""
    hosts = tuple(hosts)
    if not hosts or any(not _HOST.fullmatch(h) for h in hosts) or not 1 <= samples <= 20 or not 0 <= interval <= 300:
        raise ValueError('provider drift input invalid')
    resolved = {h: set() for h in hosts}
    for n in range(samples):
        if n:
            sleep(interval)
        for host in hosts:
            resolved[host] |= set(resolver(host))
    seen = set().union(*resolved.values())
    missing = sorted(seen - allowlist, key=ipaddress.IPv4Address)
    details = {'hosts': {h: {'resolved': sorted(a, key=ipaddress.IPv4Address), 'missing': sorted(a - allowlist, key=ipaddress.IPv4Address)}
                         for h, a in resolved.items()},
               'missing': missing, 'extra': sorted(allowlist - seen, key=ipaddress.IPv4Address),
               'unresolved_hosts': [h for h, a in resolved.items() if not a], 'samples': samples}
    checks = {'provider_hosts_resolve': not details['unresolved_hosts'], 'provider_addresses_allowed': not missing}
    if nft_text is not None:
        rules = {source: sorted(addresses, key=ipaddress.IPv4Address) for source, addresses in rule_sets(nft_text).items()}
        stale = {source: {'missing': sorted(allowlist - set(a), key=ipaddress.IPv4Address), 'extra': sorted(set(a) - allowlist, key=ipaddress.IPv4Address)}
                 for source, a in rules.items() if set(a) != allowlist}
        details['nft_rules'] = {'egress_rules': len(rules), 'stale': stale}
        checks['nft_rules_match_allowlist'] = bool(rules) and not stale
    return {'checks': checks, 'details': details}
