#!/usr/bin/env python3
"""Host health (E2/E3, docs/implementation/host-health.md §4): set the passwords of the host recorder roles, on DATA_HOST, as
nexloop_bootstrap over the local socket.

    printf 'data_host\\t%s\\napp_host\\t%s\\n' "$DATA_PW" "$APP_PW" | python3 host_recorder_password.py --bootstrap-dsn-file F

stdin: one `data_host<TAB>password` and/or `app_host<TAB>password` line (never argv, never printed). Each password reaches
PostgreSQL only as a SCRAM-SHA-256 verifier computed here (configurator_window.scram_verifier, the stage_secrets form):
`ALTER ROLE nexloop_host_recorder_<host> PASSWORD '<verifier>'`. The bootstrap DSN file is 0600 and the caller's, and must name
nexloop_bootstrap over the local socket (host=/...). Output: one JSON line naming the roles set. Exit 0 set, 2 refused.
Run it again with a new password to rotate; the recorder DSN on that host must then be rewritten (host_health_install.py does).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packages/eios-core/src'))

ROLES = {'data_host': 'nexloop_host_recorder_data_host', 'app_host': 'nexloop_host_recorder_app_host'}
VERIFIER = re.compile(r'SCRAM-SHA-256\$[0-9]+:[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+')


class Refused(ValueError):
    pass


def read_passwords(stream):
    out = {}
    for line in stream.read(1 << 14).splitlines():
        if not line.strip():
            continue
        host, sep, password = line.partition('\t')
        if not sep or host not in ROLES or host in out or not 16 <= len(password) <= 256 or any(c in password for c in '\r\n\0'):
            raise Refused('stdin: data_host<TAB>password and/or app_host<TAB>password, 16-256 characters each')
        out[host] = password
    if not out:
        raise Refused('stdin: no password')
    return out


def bootstrap_dsn(path):
    from nexloop_eios.private_configuration import read_private_text
    from psycopg.conninfo import conninfo_to_dict
    try:
        dsn = read_private_text(path, maximum=16384).strip()
    except Exception:  # noqa: BLE001 - the file is named, never its content
        raise Refused('--bootstrap-dsn-file: unreadable or not a 0600 file of the caller') from None
    params = conninfo_to_dict(dsn)
    if params.get('user') != 'nexloop_bootstrap' or not str(params.get('host', '')).startswith('/'):
        raise Refused('--bootstrap-dsn-file: the nexloop_bootstrap DSN over the local socket (host=<socket directory>)')
    # The DSN of the container (host=/var/run/postgresql) is not the host's socket directory (/srv/nexloop/run/postgresql on stage):
    # say so before connecting instead of a bare OperationalError.
    socket = Path(params['host']) / f'.s.PGSQL.{params.get("port") or 5432}'
    if not socket.exists():
        raise Refused(f'--bootstrap-dsn-file: no PostgreSQL socket {socket.name} in the socket directory {params["host"]} '
                      '(on the host it is the mounted directory, e.g. /srv/nexloop/run/postgresql, not the container\'s /var/run/postgresql)')
    return dsn


def set_passwords(connect, passwords, verifier=None):
    if verifier is None:
        from configurator_window import scram_verifier as verifier
    done = []
    with connect() as db:
        for host, password in sorted(passwords.items()):
            value = verifier(password)
            if not VERIFIER.fullmatch(value):  # the literal below carries the verifier only, never the password
                raise Refused('verifier shape')
            db.execute(f"alter role {ROLES[host]} password '{value}'")
            done.append(ROLES[host])
    return done


def main(argv=None, stdin=sys.stdin, out=sys.stdout, connect=None):
    p = argparse.ArgumentParser(description='Set the host recorder role passwords (SCRAM verifiers, bootstrap over the local socket)')
    p.add_argument('--bootstrap-dsn-file', required=True)
    a = p.parse_args(argv)
    try:
        passwords = read_passwords(stdin)
        if connect is None:
            import psycopg
            dsn = bootstrap_dsn(a.bootstrap_dsn_file)
            connect = lambda: psycopg.connect(dsn, autocommit=True, connect_timeout=10)  # noqa: E731
        roles = set_passwords(connect, passwords)
    except Refused as error:
        print(json.dumps({'refused': str(error)}), file=out)
        return 2
    except Exception as error:  # noqa: BLE001 - never a password or DSN: the type only
        print(json.dumps({'refused': 'database ' + type(error).__name__}), file=out)
        return 2
    print(json.dumps({'set': roles}), file=out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
