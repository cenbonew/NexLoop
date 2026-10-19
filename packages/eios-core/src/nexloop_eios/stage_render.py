"""NX-032 slice 0: whitelist renderer for the stage configuration templates (docs/implementation/NX-032-design.md §1.2, §2, §3).

Templates carry placeholders `__NAME__`. Only the parameters below exist; each value comes from the private env file of the
host (KEY=VALUE, 0600) and is checked against its kind before it is written. Rendering refuses: an unknown placeholder in a
template, a parameter the template needs but the env file does not give, a value that does not match its kind (no newline,
no space, no quote, no path traversal), and any `__X__` left in the output. Nothing here names a real host, address, user or
path; nothing connects anywhere.

    scripts/deploy/render.py --env <private.env> --template <file.template> --out <file>   (refuses an existing output)
    scripts/deploy/render.py --env <private.env> --check                                   (validates the env file only)
    scripts/deploy/render.py --nft-transaction <rendered rules file> --out <file>          (G-NFT-PERSIST: the boot load file)

--nft-transaction writes the file nexloop-nft.service loads with `nft -f` (deploy/stage/systemd/nexloop-nft.service.example): one
transaction that declares, deletes and recreates the inet nexloop_stage table from the rendered rules (`include`), so a load
replaces exactly that table atomically and never touches another table (UFW, Docker). Both files: root, 0600, absolute paths.

Prints one JSON object (parameter names only, never values). Exit codes: 0 ok, 2 refused.
"""
import argparse
import ipaddress
import json
import os
import re
import stat
import sys
from pathlib import Path

PLACEHOLDER = re.compile(r'__([A-Z][A-Z0-9_]*)__')
_NAME = re.compile(r'[a-z][a-z0-9._-]{0,62}')
_HOST = re.compile(r'(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*')
_ROLE = re.compile(r'nexloop_[a-z][a-z0-9_]{0,40}')
_SIZE = re.compile(r'[1-9][0-9]{0,5}(kb|mb|gb)')
_IMAGE = re.compile(r'[a-z0-9][a-z0-9._/:-]{0,200}@sha256:[0-9a-f]{64}')


class Refused(ValueError):
    pass


def _addr(v):
    ipaddress.ip_address(v)
    return v


def _cidr(v):
    ipaddress.ip_network(v, strict=True)
    return v


def _port(v):
    if not re.fullmatch(r'[1-9][0-9]{0,4}', v) or not 1024 <= int(v) <= 65535:
        raise ValueError()
    return v


def _remote_port(v):
    """A port on another host (the enterprise endpoint, NX-037): any registered port, 443 included."""
    if not re.fullmatch(r'[1-9][0-9]{0,4}', v) or not 1 <= int(v) <= 65535:
        raise ValueError()
    return v


def _path(v):
    if not re.fullmatch(r'/[A-Za-z0-9._/-]{1,240}', v) or '/../' in v + '/' or '//' in v or v.endswith('/'):
        raise ValueError()
    return v


def _match(pattern):
    def check(v):
        if not pattern.fullmatch(v):
            raise ValueError()
        return v
    return check


def _hostname_or_addr(v):
    try:
        return _addr(v)
    except ValueError:
        return _match(_HOST)(v)


def _roles(v):
    roles = v.split(',')
    if not roles or len(set(roles)) != len(roles) or not all(_ROLE.fullmatch(r) for r in roles):
        raise ValueError()
    return v


def _addresses(v):
    for part in v.split(','):
        _addr(part)
    return v


def _cidrs_or_empty(v):
    """A comma-separated list of CIDRs, or empty (the optional rule is then left out)."""
    if v == '':
        return v
    parts = v.split(',')
    if len(set(parts)) != len(parts):
        raise ValueError()
    for part in parts:
        _cidr(part)
    return ', '.join(parts)


def _uid(v):
    if not re.fullmatch(r'[1-9][0-9]{2,8}', v):
        raise ValueError()
    return v


# The whitelist: parameter → kind. A template may use only these.
PARAMETERS = {
    'APP_HOST_ADDR': _addr, 'DATA_HOST_ADDR': _addr, 'APP_HOST_NAME': _hostname_or_addr, 'DATA_HOST_NAME': _hostname_or_addr,
    'TRUSTED_LAN_CIDR': _cidr, 'MGMT_SOURCE_CIDR': _cidr, 'APP_CONTAINER_CIDR': _cidr, 'AGENT_EGRESS_CIDR': _cidr,
    'PG_PORT': _port, 'VALKEY_PORT': _port, 'GATEWAY_HTTPS_PORT': _port, 'GATEWAY_REDIRECT_PORT': _port,
    'API_INTERNAL_PORT': _port, 'API_INTERNAL_PORT_2': _port, 'HOST_INTERNAL_PORT': _port, 'GUARD_PORT': _port,
    'NEXLOOP_ROOT': _path, 'NEXLOOP_DB': _match(_NAME), 'APP_PG_ROLES': _roles, 'MIGRATION_PG_ROLE': _match(_ROLE),
    'VALKEY_MAXMEMORY': _match(_SIZE), 'AGENT_HOST_UID': _uid, 'CI_UID': _uid, 'MODEL_PROVIDER_ALLOWLIST': _addresses,
    # NX-037 provider routing: the effect worker's own egress network and the one enterprise endpoint it may reach.
    'EFFECT_EGRESS_CIDR': _cidr, 'ENTERPRISE_ENDPOINT_ADDR': _addr, 'ENTERPRISE_ENDPOINT_PORT': _remote_port,
    # G-CLAIM-EGRESS: the claim workers' own egress network (model allowlist and DATA_HOST PostgreSQL).
    'CLAIM_EGRESS_CIDR': _cidr,
    # NX-035 M8: the pgBackRest TLS server on APP_HOST (repo1); only DATA_HOST may connect to it.
    'PGBACKREST_TLS_PORT': _port,
    # Owner ruling 2026-10-15 (run sheet 4.2): the reference endpoint published on APP_HOST admits the effect worker network only.
    'REFERENCE_ENDPOINT_PORT': _port,
    # Calibration 2026-10-17: trusted staff networks that may open the reference endpoint's staff page (same port). Optional: absent
    # or empty, every line using it is left out, so the staff page stays closed like the rest of the endpoint (default deny).
    'STAFF_ALLOW_CIDRS': _cidrs_or_empty,
    # 2026-10-19 (provider drift): the claim egress namespace's own model allowlist (the claim workers and the recall indexer also
    # call the Ark embedding endpoint; the Agent Host must not). Optional: absent or empty, it is MODEL_PROVIDER_ALLOWLIST, so a
    # stage env without it renders exactly as before.
    'CLAIM_MODEL_PROVIDER_ALLOWLIST': lambda v: v if v == '' else _addresses(v),
}
# Derived in render() (never in an env file): every model address of any namespace, for the rules that keep the application and
# effect namespaces away from model addresses.
DERIVED_PARAMETERS = frozenset({'MODEL_PROVIDER_ALLOWLIST_ALL'})


def derived(values):
    """The env values plus the derived ones: CLAIM_MODEL_PROVIDER_ALLOWLIST defaults to MODEL_PROVIDER_ALLOWLIST; the union keeps
    MODEL_PROVIDER_ALLOWLIST's order and appends the claim-only addresses (equal to it when the claim list adds nothing)."""
    out = dict(values)
    base = values.get('MODEL_PROVIDER_ALLOWLIST')
    if base is None:
        return out
    if not values.get('CLAIM_MODEL_PROVIDER_ALLOWLIST'):
        out['CLAIM_MODEL_PROVIDER_ALLOWLIST'] = base
    parts = base.split(',')
    parts += [a for a in out['CLAIM_MODEL_PROVIDER_ALLOWLIST'].split(',') if a not in parts]
    out['MODEL_PROVIDER_ALLOWLIST_ALL'] = ','.join(parts)
    return out
# Optional parameters: a template line that uses one is rendered only when the env file gives it a non-empty value.
OPTIONAL_LINE_PARAMETERS = frozenset({'STAFF_ALLOW_CIDRS'})


def load_env(path):
    """KEY=VALUE lines; the file must be a regular, owner-only file. Unknown keys are refused (a typo never passes silently)."""
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise Refused('env file must be a regular file with mode 0600')
    values, unknown, invalid = {}, [], []
    for line in Path(path).read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, sep, value = line.partition('=')
        key, value = key.strip(), value.strip()
        if not sep or key in values:
            raise Refused('env file line invalid')
        if key not in PARAMETERS:
            unknown.append(key)
            continue
        try:
            values[key] = PARAMETERS[key](value)
        except ValueError:
            invalid.append(key)
    if unknown or invalid:
        raise Refused(json.dumps({'unknown': sorted(unknown), 'invalid': sorted(invalid)}))
    return values


def render(text, values):
    values = derived(values)
    absent = {name for name in OPTIONAL_LINE_PARAMETERS if not values.get(name)}
    if absent:
        text = ''.join(line for line in text.splitlines(keepends=True) if not any(f'__{name}__' in line for name in absent))
    needed = set(PLACEHOLDER.findall(text))
    unknown = sorted(needed - set(PARAMETERS) - DERIVED_PARAMETERS)
    missing = sorted(needed & (set(PARAMETERS) | DERIVED_PARAMETERS) - set(values))
    if unknown or missing:
        raise Refused(json.dumps({'unknown_placeholders': unknown, 'missing_parameters': missing}))
    out = PLACEHOLDER.sub(lambda m: values[m.group(1)], text)
    if PLACEHOLDER.search(out):
        raise Refused('placeholder left after rendering')
    return out, sorted(needed)


NFT_TABLE = 'inet nexloop_stage'
_NFT_PATH = re.compile(r'/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+')


def nft_transaction(rules_path):
    """The atomic load file: `table` (creates the table if absent, so `delete` never fails on a first boot), `delete table`, then
    the rendered rules (which recreate it). `nft -f` applies the whole file as one transaction or nothing."""
    path = str(rules_path)
    if not _NFT_PATH.fullmatch(path) or '..' in path.split('/'):
        raise Refused('rules path must be absolute, without spaces, quotes or ..')
    return (f'# G-NFT-PERSIST: generated by render.py --nft-transaction; loaded by nexloop-nft.service (nft -f), one transaction.\n'
            f'table {NFT_TABLE}\ndelete table {NFT_TABLE}\ninclude "{path}"\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--nft-transaction', type=Path, help='write the boot load file for this rendered rules file to --out')
    parser.add_argument('--env', type=Path)
    parser.add_argument('--template', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.nft_transaction is not None:
            if not args.out:
                raise Refused('--out required')
            if args.out.exists():
                raise Refused('output exists')
            text = nft_transaction(args.nft_transaction)
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(text)
            print(json.dumps({'rendered': True, 'nft_transaction': True, 'table': NFT_TABLE}))
            return 0
        if args.env is None:
            raise Refused('--env required')
        values = load_env(args.env)
        if args.check:
            print(json.dumps({'valid': True, 'parameters': sorted(values)}))
            return 0
        if not args.template or not args.out:
            raise Refused('--template and --out required')
        if args.out.exists():
            raise Refused('output exists')
        text, used = render(args.template.read_text(), values)
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
        print(json.dumps({'rendered': True, 'template': args.template.name, 'parameters': used}))
        return 0
    except (Refused, OSError) as error:
        print(json.dumps({'rendered': False, 'refused': str(error) if isinstance(error, Refused) else type(error).__name__}))
        return 2


if __name__ == '__main__':
    sys.exit(main())
