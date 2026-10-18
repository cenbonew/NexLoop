"""Model provider address drift (owner 2026-10-19; v0.1 known risk "the model provider egress is a static IP allowlist").

The model egress rules accept numeric addresses only (MODEL_PROVIDER_ALLOWLIST in the private stage env, rendered into the
agent and claim egress rules of app-host.rules.nft). A provider host that starts resolving to an address outside that list
fails every connection by timeout. This check, read only, resolves each model endpoint host (IPv4, several samples: CDN answers
rotate) and compares:
- missing: an address a host resolves to that the allowlist lacks (connections to it time out) -> the check fails;
- extra: an allowlisted address no host resolved to in this run (informational: it may be an earlier answer of a rotating CDN);
- the rendered rules, per namespace (agent egress: DeepSeek; claim egress: DeepSeek and the Ark embedding endpoint): exactly one
  443 egress rule from the namespace's source CIDR, carrying every address its own providers resolve to and nothing outside the
  allowlist (a missing rule, a rule rendered from an older env or for the wrong providers is a drift too); addresses a rule has
  beyond its providers' current answers are reported as extra (CDN rotation), not failed.
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


# PR #14 review: each model egress namespace has its own expected providers (stage: the Agent Host calls DeepSeek only; the claim
# workers and the recall indexer also call the Ark embedding endpoint), keyed by the stage env variable of its source CIDR.
DEFAULT_NAMESPACES = {'agent': ('AGENT_EGRESS_CIDR', ('api.deepseek.com',)),
                      'claim': ('CLAIM_EGRESS_CIDR', ('api.deepseek.com', 'ark.cn-beijing.volces.com'))}


def parse_namespace(text):
    """`name=host[,host...]` (a --namespace option) -> (name, hosts)."""
    name, sep, hosts = text.partition('=')
    hosts = tuple(h.strip() for h in hosts.split(',') if h.strip())
    if not sep or name not in DEFAULT_NAMESPACES or not hosts or any(not _HOST.fullmatch(h) for h in hosts):
        raise ValueError('namespace invalid')
    return name, hosts


def check(allowlist, namespaces, *, cidrs, resolver=system_resolver, samples=1, interval=0.0, sleep=time.sleep, nft_text=None):
    """{checks, details} for doctor.
    allowlist: the env's MODEL_PROVIDER_ALLOWLIST (set); namespaces: name -> provider hosts; cidrs: name -> its source CIDR (from
    the stage env); nft_text: the rendered rules (optional). Every resolved address must be in the allowlist; with the rules, each
    namespace must have exactly one 443 egress rule from its CIDR that carries every address its own hosts resolve to."""
    hosts = tuple(sorted({h for hs in namespaces.values() for h in hs}))
    if (not namespaces or set(namespaces) - set(DEFAULT_NAMESPACES) or not hosts or any(not _HOST.fullmatch(h) for h in hosts)
            or not 1 <= samples <= 20 or not 0 <= interval <= 300 or set(namespaces) - set(cidrs)):
        raise ValueError('provider drift input invalid')
    resolved = {h: set() for h in hosts}
    for n in range(samples):
        if n:
            sleep(interval)
        for host in hosts:
            resolved[host] |= set(resolver(host))
    key = ipaddress.IPv4Address
    seen = set().union(*resolved.values())
    missing = sorted(seen - allowlist, key=key)
    details = {'hosts': {h: {'resolved': sorted(a, key=key), 'missing': sorted(a - allowlist, key=key)} for h, a in resolved.items()},
               'missing': missing, 'extra': sorted(allowlist - seen, key=key),
               'unresolved_hosts': [h for h, a in resolved.items() if not a], 'samples': samples}
    checks = {'provider_hosts_resolve': not details['unresolved_hosts'], 'provider_addresses_allowed': not missing}
    if nft_text is not None:
        found = {}
        for source, body in _RULE.findall(nft_text):
            found.setdefault(source, []).append({a.strip() for a in body.split(',') if a.strip()})
        per = {}
        for name, names in sorted(namespaces.items()):
            expected = set().union(*(resolved[h] for h in names))
            rules = found.get(cidrs[name], [])
            addresses = rules[0] if len(rules) == 1 else set()
            per[name] = {'cidr': cidrs[name], 'rules': len(rules), 'providers': list(names),
                         'missing': sorted(expected - addresses, key=key), 'extra': sorted(addresses - expected, key=key),
                         'outside_allowlist': sorted(addresses - allowlist, key=key)}
        details['nft_rules'] = per
        checks['nft_rules_complete'] = all(v['rules'] == 1 and not v['missing'] and not v['outside_allowlist'] for v in per.values())
    return {'checks': checks, 'details': details}
