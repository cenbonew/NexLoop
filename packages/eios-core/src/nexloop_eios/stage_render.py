"""NX-032 slice 0: whitelist renderer for the stage configuration templates (docs/implementation/NX-032-design.md §1.2, §2, §3).

Templates carry placeholders `__NAME__`. Only the parameters below exist; each value comes from the private env file of the
host (KEY=VALUE, 0600) and is checked against its kind before it is written. Rendering refuses: an unknown placeholder in a
template, a parameter the template needs but the env file does not give, a value that does not match its kind (no newline,
no space, no quote, no path traversal), and any `__X__` left in the output. Nothing here names a real host, address, user or
path; nothing connects anywhere.

    scripts/deploy/render.py --env <private.env> --template <file.template> --out <file>   (refuses an existing output)
    scripts/deploy/render.py --env <private.env> --check                                   (validates the env file only)

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
    # NX-035 M8: the pgBackRest TLS server on APP_HOST (repo1); only DATA_HOST may connect to it.
    'PGBACKREST_TLS_PORT': _port,
    # Owner ruling 2026-10-15 (run sheet 4.2): the reference endpoint published on APP_HOST admits the effect worker network only.
    'REFERENCE_ENDPOINT_PORT': _port,
}


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
    needed = set(PLACEHOLDER.findall(text))
    unknown = sorted(needed - set(PARAMETERS))
    missing = sorted(needed & set(PARAMETERS) - set(values))
    if unknown or missing:
        raise Refused(json.dumps({'unknown_placeholders': unknown, 'missing_parameters': missing}))
    out = PLACEHOLDER.sub(lambda m: values[m.group(1)], text)
    if PLACEHOLDER.search(out):
        raise Refused('placeholder left after rendering')
    return out, sorted(needed)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--env', type=Path, required=True)
    parser.add_argument('--template', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    try:
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
