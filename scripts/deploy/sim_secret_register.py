#!/usr/bin/env python3
"""Simulation stage enablement: register the simulation group's two keys in the database - the signing key (authz.nexloop_authority_
signing_keys, through the trusted configuration, as NX-032's signing-key and S6b step 3) and the simulation Host's purge key
(authz.nexloop_sim_hosts, the owner's insert, S6 step 3) - then self-check. Runs on DATA_HOST in the sim-issue one-off container over
the PostgreSQL socket (bootstrap: local pg_hba rules only; docs/implementation/sim-stage-enable.md).

    sim_secret_register.py --config /setup/PRIVATE.json --dry-run   what is registered now (read only); writes nothing
    sim_secret_register.py --config /setup/PRIVATE.json             reads the keys on stdin - only as the DATA_HOST end of the pipeline
        ssh APP_HOST '... sim_secret_files.py --export-registration' | ssh DATA_HOST '... sim_secret_register.py --config ...'
        run on the dispatcher's machine (the hosts never trust each other) - checks them against the digests APP_HOST printed
        (expect), registers what is missing (an identical registration is kept; other material under the same id is refused: key
        ids and Host keys are immutable), then self-checks
    sim_secret_register.py --config /setup/PRIVATE.json --check     the self-check alone (read only)

Self-check: the signing key id is a simulation one (sim-...), active, its material the one APP_HOST holds (sha256), and held by no
other key id; the Host is active with the purge key APP_HOST holds (sha256). The table has no world-group column: "the simulation
group's own key" is checked as these facts (its own id and material), not as a database attribute.
The signing key is registered through a configurator window opened with the bootstrap DSN (configurator_window: nexloop_configurator
logs in for the publish only), the Host by the bootstrap session; the key material passes through a private file in the container's
/tmp for the trusted configuration's reader and is removed at once. Output: one JSON line; never a key or a DSN.

Configuration (private JSON, 0600; deploy/examples/sim-secret-register.example.json):
  {"schema": "nexloop-sim-secret-register/1", "tenant_id", "bootstrap_dsn_file": "/setup/bootstrap-dsn" (nexloop_bootstrap, host=/socket),
   "signing_key_id": "sim-...", "host_id", "expect": {"signing_key_sha256", "purge_key_sha256"} (sim_secret_files.py's registration)}
Exit: 0 done, 1 self-check failed or refused by the database, 2 input refused.
"""
import argparse
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
sys.path.insert(0, str(ROOT / 'packages/eios-core/src'))

SCHEMA = 'nexloop-sim-secret-register/1'
KEYS = {'schema', 'tenant_id', 'bootstrap_dsn_file', 'signing_key_id', 'host_id', 'expect'}
_HEX64 = re.compile(r'[0-9a-f]{64}')
_KEY_ID = re.compile(r'sim-[A-Za-z0-9._-]{1,60}')
_HOST = re.compile(r'[a-z][a-z0-9-]{2,62}')
_TENANT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')


class Refused(ValueError):
    pass


def load_config(text):
    try:
        value = json.loads(text)
    except ValueError:
        raise Refused('configuration is not JSON') from None
    if type(value) is not dict or value.get('schema') != SCHEMA or set(value) != KEYS:
        raise Refused('configuration fields: ' + ', '.join(sorted(KEYS - {'schema'})))
    if type(value['tenant_id']) is not str or not _TENANT.fullmatch(value['tenant_id']):
        raise Refused('tenant_id invalid')
    if type(value['signing_key_id']) is not str or not _KEY_ID.fullmatch(value['signing_key_id']):
        raise Refused('signing_key_id: sim-<id>')
    if type(value['host_id']) is not str or not _HOST.fullmatch(value['host_id']):
        raise Refused('host_id invalid')
    expect = value['expect']
    if type(expect) is not dict or set(expect) != {'signing_key_sha256', 'purge_key_sha256'} or not all(type(v) is str and _HEX64.fullmatch(v) for v in expect.values()):
        raise Refused('expect: {"signing_key_sha256", "purge_key_sha256"} from sim_secret_files.py')
    return value


def bootstrap_dsn(config):
    from psycopg.conninfo import conninfo_to_dict
    from nexloop_eios.trusted_configuration import read_private_text
    text = read_private_text(config['bootstrap_dsn_file'], maximum=16384).strip()
    params = conninfo_to_dict(text)
    if params.get('user') != 'nexloop_bootstrap' or not str(params.get('host', '')).startswith('/'):
        raise Refused('bootstrap_dsn_file: the nexloop_bootstrap DSN over the local socket (host=/...)')
    return text


def state(db, config):
    """Read only: the current rows (digests and flags), and any other key id holding the same signing material."""
    db.execute('set transaction read only')
    key = db.execute("select encode(sha256(key_material),'hex'),active from authz.nexloop_authority_signing_keys where key_id=%s",
                     (config['signing_key_id'],)).fetchone()
    shared = db.execute("""select count(*) from authz.nexloop_authority_signing_keys a join authz.nexloop_authority_signing_keys b
        on a.key_material=b.key_material and a.key_id<>b.key_id where a.key_id=%s""", (config['signing_key_id'],)).fetchone()[0]
    host = db.execute("select encode(sha256(key_material),'hex'),active from authz.nexloop_sim_hosts where host_id=%s", (config['host_id'],)).fetchone()
    return {'signing_key': key, 'signing_material_shared': shared, 'host': host}


def check(current, config):
    e = config['expect']
    out = {'signing_key_is_simulation': config['signing_key_id'].startswith('sim-'),
           'signing_key_active': bool(current['signing_key'] and current['signing_key'][1]),
           'signing_key_material_is_app_host_s': bool(current['signing_key']) and current['signing_key'][0] == e['signing_key_sha256'],
           'signing_material_held_by_no_other_key': current['signing_material_shared'] == 0,
           'host_active': bool(current['host'] and current['host'][1]),
           'host_purge_key_is_app_host_s': bool(current['host']) and current['host'][0] == e['purge_key_sha256']}
    return {'pass': all(out.values()), 'items': out}


def read_registration(stream, config):
    try:
        value = json.loads(stream.read(4096))
    except ValueError:
        raise Refused('stdin: the registration line of sim_secret_files.py --export-registration') from None
    if type(value) is not dict or value.get('schema') != 'nexloop-sim-registration/1' or set(value) != {'schema', 'signing_key_id', 'signing_key_hex', 'host_id', 'purge_key_hex'}:
        raise Refused('stdin: the registration line of sim_secret_files.py --export-registration')
    if value['signing_key_id'] != config['signing_key_id'] or value['host_id'] != config['host_id']:
        raise Refused('stdin: another key id or host id than this configuration')
    try:
        signing, purge = bytes.fromhex(value['signing_key_hex']), bytes.fromhex(value['purge_key_hex'])
    except (TypeError, ValueError):
        raise Refused('stdin: keys in hex') from None
    if len(signing) != 32 or len(purge) != 32:
        raise Refused('stdin: 32-byte keys')
    if hashlib.sha256(signing).hexdigest() != config['expect']['signing_key_sha256'] or hashlib.sha256(purge).hexdigest() != config['expect']['purge_key_sha256']:
        raise Refused('stdin: the keys are not the ones APP_HOST reported (expect digests)')
    return signing, purge


def register(config, signing, purge, dsn, connect):
    import stage_secrets
    from configurator_window import configurator_window
    with connect(dsn) as db, db.transaction():
        current = state(db, config)
    changed = []
    if current['signing_key'] is None or not current['signing_key'][1] or current['signing_key'][0] != config['expect']['signing_key_sha256']:
        with tempfile.TemporaryDirectory(prefix='sim-register-') as work:
            (Path(work) / 'management').mkdir(mode=0o700)
            hex_file = Path(work) / 'management' / 'signing.key.hex'
            stage_secrets.write_private(hex_file, signing.hex())
            existing = None if current['signing_key'] is None else (None, current['signing_key'][1])
            if existing is not None:  # stage_secrets refuses other material under the id, and never reactivates
                raise Refused('the signing key id is registered with other material or inactive: choose a new sim- key id')
            with configurator_window(config['bootstrap_dsn_file'], refused=Refused) as configurator:
                stage_secrets.register_signing_key(work, tenant=config['tenant_id'], signing_key_id=config['signing_key_id'], database_url_file=configurator)
        changed.append('signing_key')
    if current['host'] is None:
        with connect(dsn) as db:
            db.execute('insert into authz.nexloop_sim_hosts(host_id,key_material) values(%s,%s)', (config['host_id'], purge))
        changed.append('host')
    elif current['host'][0] != config['expect']['purge_key_sha256'] or not current['host'][1]:
        raise Refused('the host id is registered with another purge key or inactive (Host keys are immutable): choose a new host id')
    return changed


def main(argv=None, *, out=sys.stdout, stdin=None, connect=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.error = lambda message: (_ for _ in ()).throw(SystemExit(2))  # never echo the arguments
    parser.add_argument('--config', required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--check', action='store_true')
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2

    def emit(value, code):
        print(json.dumps(value, sort_keys=True), file=out)
        return code
    import psycopg
    connect = connect or (lambda dsn: psycopg.connect(dsn, connect_timeout=5, autocommit=True))
    try:
        from nexloop_eios.trusted_configuration import read_private_text
        config = load_config(read_private_text(args.config, maximum=65536))
        dsn = bootstrap_dsn(config)
        if args.dry_run or args.check:
            with connect(dsn) as db, db.transaction():
                current = state(db, config)
            verdict = check(current, config)
            if args.dry_run:
                return emit({'ok': True, 'dry_run': True, 'signing_key_registered': current['signing_key'] is not None,
                             'host_registered': current['host'] is not None, 'check': verdict}, 0)
            return emit({'ok': verdict['pass'], 'check': verdict}, 0 if verdict['pass'] else 1)
        signing, purge = read_registration(stdin or sys.stdin, config)
        changed = register(config, signing, purge, dsn, connect)
        with connect(dsn) as db, db.transaction():
            verdict = check(state(db, config), config)
        return emit({'ok': verdict['pass'], 'registered': changed, 'changed': bool(changed), 'check': verdict}, 0 if verdict['pass'] else 1)
    except Refused as error:
        return emit({'ok': False, 'detail': str(error)}, 2)
    except Exception as error:  # noqa: BLE001 - the type only (a message may hold a value)
        return emit({'ok': False, 'error': type(error).__name__}, 1)


if __name__ == '__main__':
    sys.exit(main())
