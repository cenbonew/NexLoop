#!/usr/bin/env python3
"""Simulation process group stage enablement: the DSN file of every simulation login role, written 0600 into the simulation secret
directory on APP_HOST (docs/implementation/sim-stage-enable.md). Owner rule: a configuration file and one command; the passwords come
from standard input (one `<role> <password>` line per role, e.g. piped from the Keychain), never from an argument.

    sim_dsn_files.py --config PRIVATE.json --dry-run          the files it would write; reads no password, writes nothing
    for r in nexloop_sim_worker nexloop_sim_guard nexloop_sim_api nexloop_stand_in_worker nexloop_sim_files; do
      printf '%s %s\\n' "$r" "$(security find-generic-password -s <service> -a "$r" -w)"; done | sim_dsn_files.py --config PRIVATE.json [--check]

Configuration (private JSON, 0600, the caller's; deploy/examples/sim-dsn-files.example.json):
    {"schema": "nexloop-sim-dsn/1", "secret_dir": "<NEXLOOP_SIM_SECRET_DIR>", "data_host_name": "<the certificate's name>",
     "data_host_addr": "<DATA_HOST address>", "pg_port": 55432, "db_name": "nexloop_stage"}
The files are the simulation project's DSN secrets (post_deploy_check.SIM_DSN_ROLES: sim_worker_dsn, sim_guard_dsn, sim_api_dsn,
sim_stand_in_worker_dsn, sim_files_dsn), each `host=... hostaddr=... port=... dbname=... user=<role> password=... sslmode=verify-full
sslrootcert=/run/secrets/sim_pg_ca` (the CA the compose project mounts as sim_pg_ca; the pg_hba simulation line needs no client
certificate). A file is replaced atomically (0600, never through a link). --check then runs post_deploy_check.check_sim_roles on the
written files (each logs in as exactly its simulation role): run it where the DSNs resolve - a one-off container of the simulation
project, which mounts the CA at /run/secrets/sim_pg_ca and sits on the simulation subnet the APP_HOST firewall admits. Output: one JSON line of file
names, roles and counts; never a password or a DSN. Exit: 0 done, 1 check failed, 2 input refused.
"""
import argparse
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packages/eios-core/src'))
from post_deploy_check import SIM_DSN_ROLES  # noqa: E402

SCHEMA = 'nexloop-sim-dsn/1'
KEYS = {'schema', 'secret_dir', 'data_host_name', 'data_host_addr', 'pg_port', 'db_name'}
CA_IN_CONTAINER = '/run/secrets/sim_pg_ca'
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}')
_ADDR = re.compile(r'[0-9]{1,3}(\.[0-9]{1,3}){3}|[0-9a-fA-F:]{2,39}')
_DB = re.compile(r'[a-z_][a-z0-9_]{0,62}')


class Refused(ValueError):
    pass


def read_private(path, maximum=65536):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except (OSError, TypeError):
        raise Refused('private file unreadable') from None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise Refused('private file must be a regular 0600 file of the caller')
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise Refused('private file too large')
    return data.decode('utf-8')


def load_config(text):
    try:
        value = json.loads(text)
    except ValueError:
        raise Refused('configuration is not JSON') from None
    if type(value) is not dict or value.get('schema') != SCHEMA or set(value) != KEYS:
        raise Refused('configuration fields: ' + ', '.join(sorted(KEYS - {'schema'})))
    if type(value['secret_dir']) is not str or not value['secret_dir'].startswith('/'):
        raise Refused('secret_dir: an absolute path')
    if type(value['data_host_name']) is not str or not _NAME.fullmatch(value['data_host_name']):
        raise Refused('data_host_name: a host name')
    if type(value['data_host_addr']) is not str or not _ADDR.fullmatch(value['data_host_addr']):
        raise Refused('data_host_addr: an address')
    if type(value['pg_port']) is not int or not 1 <= value['pg_port'] <= 65535:
        raise Refused('pg_port: a port')
    if type(value['db_name']) is not str or not _DB.fullmatch(value['db_name']):
        raise Refused('db_name: a database name')
    return value


def quote(value):
    """A libpq connection-string value: single-quoted, backslash and quote escaped."""
    return "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"


def read_passwords(stream):
    """`<role> <password>` lines -> {role: password}; every simulation role once, nothing else."""
    out = {}
    for line in stream.read(65536).splitlines():
        if not line.strip():
            continue
        role, _, password = line.partition(' ')
        if role not in SIM_DSN_ROLES.values() or role in out:
            raise Refused('password lines: each simulation role once (<role> <password>)')
        if not 16 <= len(password) <= 512 or password != password.strip() or '\n' in password:
            raise Refused(f'the password of {role}: 16-512 characters, no surrounding space')
        out[role] = password
    missing = sorted(set(SIM_DSN_ROLES.values()) - set(out))
    if missing:
        raise Refused('no password line for ' + ', '.join(missing))
    return out


def dsn(config, role, password):
    return (f"host={config['data_host_name']} hostaddr={config['data_host_addr']} port={config['pg_port']} dbname={config['db_name']} "
            f"user={role} password={quote(password)} sslmode=verify-full sslrootcert={CA_IN_CONTAINER}")


def write_private(directory, name, text):
    directory = Path(directory)
    target = directory / name
    if target.is_symlink():
        raise Refused(f'{name} is a link')
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.' + name + '.')
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as out:
            out.write(text + '\n')
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main(argv=None, *, stdin=None, out=sys.stdout, check=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.error = lambda message: (_ for _ in ()).throw(SystemExit(2))  # never echo the arguments
    parser.add_argument('--config', required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--check', action='store_true')
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2

    def emit(value, code):
        print(json.dumps(value, sort_keys=True), file=out)
        return code
    try:
        config = load_config(read_private(args.config))
        directory = Path(config['secret_dir'])
        planned = [{'file': name, 'role': role} for name, role in sorted(SIM_DSN_ROLES.items())]
        if args.dry_run:
            return emit({'ok': True, 'dry_run': True, 'secret_dir_exists': directory.is_dir(), 'files': planned}, 0)
        if not directory.is_dir() or directory.is_symlink() or directory.stat().st_mode & 0o077:
            raise Refused('secret_dir: an existing 0700 directory (not a link)')
        passwords = read_passwords(stdin or sys.stdin)
        for name, role in sorted(SIM_DSN_ROLES.items()):
            write_private(directory, name, dsn(config, role, passwords[role]))
        result = {'ok': True, 'dry_run': False, 'files': planned, 'written': len(planned)}
        if args.check:
            from post_deploy_check import check_sim_roles, sim_dsn_files
            verdict = (check or check_sim_roles)(sim_dsn_files(directory), sorted(SIM_DSN_ROLES))
            result.update(ok=bool(verdict.get('pass')), check=verdict)
            return emit(result, 0 if verdict.get('pass') else 1)
        return emit(result, 0)
    except Refused as error:
        return emit({'ok': False, 'detail': str(error)}, 2)
    except Exception as error:  # noqa: BLE001 - review 3.2: the type only (a message may hold a DSN or password)
        return emit({'ok': False, 'error': type(error).__name__}, 1)


if __name__ == '__main__':
    sys.exit(main())
