#!/usr/bin/env python3
"""NX-032 slice 1 (D1): the internal CA and service certificates with openssl only (docs/implementation/NX-032-design.md §2.1).

Run on the owner's management machine. The root CA key never leaves it; the intermediate CA (1 year) signs service and client
certificates (90 days; rotate at 30 days left). Private keys are written 0600 into a new file and are never printed, logged or
passed on a command line. Names and SANs are parameters; nothing here names a real host or address.

    issue_certs.py root          --dir ROOT_DIR [--days 3650]
    issue_certs.py intermediate  --root-dir ROOT_DIR --dir CA_DIR [--days 365]
    issue_certs.py server        --ca-dir CA_DIR --out DIR --name pg-server --san DNS:<name> --san IP:<addr> [--days 90]
    issue_certs.py client        --ca-dir CA_DIR --out DIR --name pg-client-nexloop_api --cn nexloop_api [--days 90]

Outputs per certificate: <name>.key (0600), <name>.crt (leaf), <name>.chain.crt (leaf + intermediate). The CA directories hold
ca.key (0600), ca.crt, and for the intermediate also bundle.crt (intermediate + root). Prints one JSON object (paths are not
printed, only names, serials and validity). Exit 0 ok, 2 refused.
"""
import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_DAYS = {'root': 3650, 'intermediate': 365, 'server': 90, 'client': 90}
_NAME = re.compile(r'[a-z][a-z0-9_.-]{0,62}')
# PG roles and Valkey ACL users; '-' for the pgBackRest TLS client (tls-server-auth CN pgbackrest-data-client, M8).
_CN = re.compile(r'[a-z][a-z0-9_-]{0,62}')
_DNS = re.compile(r'(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*')


class Refused(ValueError):
    pass


def openssl(*args, cwd=None):
    run = subprocess.run(['openssl', *args], capture_output=True, text=True, cwd=cwd)
    if run.returncode != 0:
        raise Refused('openssl ' + args[0] + ' failed')
    return run.stdout


def new_key(path):
    """An EC P-256 key created 0600 (umask), refusing to overwrite."""
    path = Path(path)
    if path.exists():
        raise Refused('key exists: ' + path.name)
    old = os.umask(0o077)
    try:
        openssl('genpkey', '-algorithm', 'EC', '-pkeyopt', 'ec_paramgen_curve:P-256', '-out', str(path))
    finally:
        os.umask(old)
    path.chmod(0o600)
    return path


def _extfile(text):
    fd, name = tempfile.mkstemp(suffix='.cnf')
    with os.fdopen(fd, 'w') as stream:
        stream.write(text)
    return name


def _days(kind, days):
    if type(days) is not int or not 1 <= days <= MAX_DAYS[kind]:
        raise Refused(f'{kind} validity must be 1..{MAX_DAYS[kind]} days (D1)')
    return str(days)


def _info(cert):
    out = openssl('x509', '-in', str(cert), '-noout', '-serial', '-enddate', '-subject')
    fields = dict(line.split('=', 1) for line in out.splitlines() if '=' in line)
    return {'serial': fields.get('serial'), 'not_after': fields.get('notAfter')}


def root(directory, days=3650):
    d = Path(directory)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = new_key(d / 'ca.key')
    ext = _extfile('[v3]\nbasicConstraints=critical,CA:true\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n')
    try:
        openssl('req', '-x509', '-new', '-key', str(key), '-subj', '/CN=NexLoop Stage Root CA', '-days', _days('root', days),
                '-sha256', '-extensions', 'v3', '-config', ext, '-out', str(d / 'ca.crt'))
    finally:
        os.unlink(ext)
    return {'issued': 'root', **_info(d / 'ca.crt')}


def _sign(ca_dir, csr, out, days, kind, extensions):
    ca = Path(ca_dir)
    ext = _extfile('[v3]\n' + extensions)
    try:
        openssl('x509', '-req', '-in', str(csr), '-CA', str(ca / 'ca.crt'), '-CAkey', str(ca / 'ca.key'), '-CAcreateserial',
                '-days', _days(kind, days), '-sha256', '-extfile', ext, '-extensions', 'v3', '-out', str(out))
    finally:
        os.unlink(ext)


def intermediate(root_dir, directory, days=365):
    d = Path(directory)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = new_key(d / 'ca.key')
    csr = d / 'ca.csr'
    openssl('req', '-new', '-key', str(key), '-subj', '/CN=NexLoop Stage Intermediate CA', '-out', str(csr))
    _sign(root_dir, csr, d / 'ca.crt', days, 'intermediate',
          'basicConstraints=critical,CA:true,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n')
    csr.unlink()
    (d / 'bundle.crt').write_text((d / 'ca.crt').read_text() + (Path(root_dir) / 'ca.crt').read_text())
    return {'issued': 'intermediate', **_info(d / 'ca.crt')}


def _san(values):
    out = []
    for v in values:
        kind, _, value = v.partition(':')
        if kind == 'DNS' and _DNS.fullmatch(value):
            out.append(f'DNS:{value}')
        elif kind == 'IP':
            out.append(f'IP:{ipaddress.ip_address(value)}')
        else:
            raise Refused('SAN must be DNS:<name> or IP:<address>')
    if not out:
        raise Refused('at least one SAN required')
    return ','.join(out)


def leaf(kind, ca_dir, out_dir, name, *, sans=(), cn=None, days=90):
    if not _NAME.fullmatch(name or ''):
        raise Refused('certificate name invalid')
    out = Path(out_dir)
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = new_key(out / f'{name}.key')
    csr = out / f'{name}.csr'
    if kind == 'server':
        subject, ext = f'/CN={name}', (f'basicConstraints=critical,CA:false\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n'
                                        f'subjectAltName={_san(sans)}\nauthorityKeyIdentifier=keyid\n')
    else:
        if not _CN.fullmatch(cn or ''):
            raise Refused('client CN must be the role or ACL user name')
        subject, ext = f'/CN={cn}', 'basicConstraints=critical,CA:false\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=clientAuth\nauthorityKeyIdentifier=keyid\n'
    openssl('req', '-new', '-key', str(key), '-subj', subject, '-out', str(csr))
    _sign(ca_dir, csr, out / f'{name}.crt', days, kind, ext)
    csr.unlink()
    (out / f'{name}.chain.crt').write_text((out / f'{name}.crt').read_text() + (Path(ca_dir) / 'ca.crt').read_text())
    return {'issued': kind, 'name': name, **_info(out / f'{name}.crt')}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('root')
    p.add_argument('--dir', type=Path, required=True)
    p.add_argument('--days', type=int, default=3650)
    p = sub.add_parser('intermediate')
    p.add_argument('--root-dir', type=Path, required=True)
    p.add_argument('--dir', type=Path, required=True)
    p.add_argument('--days', type=int, default=365)
    for kind in ('server', 'client'):
        p = sub.add_parser(kind)
        p.add_argument('--ca-dir', type=Path, required=True)
        p.add_argument('--out', type=Path, required=True)
        p.add_argument('--name', required=True)
        p.add_argument('--days', type=int, default=90)
        if kind == 'server':
            p.add_argument('--san', action='append', default=[])
        else:
            p.add_argument('--cn', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'root':
            out = root(args.dir, args.days)
        elif args.command == 'intermediate':
            out = intermediate(args.root_dir, args.dir, args.days)
        elif args.command == 'server':
            out = leaf('server', args.ca_dir, args.out, args.name, sans=args.san, days=args.days)
        else:
            out = leaf('client', args.ca_dir, args.out, args.name, cn=args.cn, days=args.days)
    except (Refused, ValueError, OSError) as error:
        print(json.dumps({'issued': False, 'refused': str(error) if isinstance(error, Refused) else type(error).__name__}))
        return 2
    print(json.dumps(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
