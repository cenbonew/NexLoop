#!/usr/bin/env python3
"""Simulation process group stage enablement: the service credentials of one simulation world, one per use (NX-041 D10), through the
trusted configuration (docs/implementation/sim-stage-enable.md). Owner rule: a configuration file and one command.

Uses and the grants each holds (the D10 end-to-end path: tests/test_nx041_sim_pi_run_pg.py):
  api     the event intake (nexloop_sim_api): issues Run credentials, accepts and registers Runs  EXECUTE the operations queue and the effect Action
  worker  the runtime worker and its guard (nexloop_sim_worker, nexloop_sim_guard) and the stand-in executor   EXECUTE the operations queue
  agent   the Agent side                                                                          EXECUTE the effect Action
  twin    the consumer twin port (NX-042 T3, eighth batch)                                          no grant (the port checks the world credential)
Every credential is bound to this one world alone (sim_use set; the database refuses it in any other world); its principal is the
world's own (never a real or test principal). Facts are compiled by nexloop_eios.service_grants.compile_principal for the world, the diff
against the tenant's current facts is applied through the trusted configuration (0050/0274 configure_manifest, configurator role), so a
rerun writes nothing. A use the database does not accept yet (twin before the eighth batch) is refused before anything is written.

    sim_world_credentials.py --config PRIVATE.json --dry-run    the plan (credentials to create, facts to write); writes nothing
    sim_world_credentials.py --config PRIVATE.json              issues, then self-checks
    sim_world_credentials.py --config PRIVATE.json --check      self-check only

Self-check (check_sim_roles rules for credentials, with check_database_url_file - a simulation role's DSN, e.g. sim_api): each
credential authenticates in its world through authz.nexloop_service_identity_snapshot and is refused in real and test; every
credential of the world has its use, one per use, active.

Configuration (private JSON, 0600; deploy/examples/sim-world-credentials.example.json):
  {"schema": "nexloop-sim-credentials/1", "tenant_id", "world": "sim:<uuid>", "uses": ["api", "worker", "agent"], "token_dir",
   "configurator_database_url_file", "signing_key_file", "signing_key_id", "credential_expires_at": "<ISO UTC>",
   "check_database_url_file", "compose_secret_dir" (optional: writes sim_world_credential = the worker token for the compose project)}
Tokens: <token_dir>/<world uuid>/<use>-credential (0600, created once, never printed). Output: one JSON line; never a token or DSN.
Exit: 0 done, 1 self-check failed or refused by the database, 2 input refused.
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'packages/eios-core/src'))

SCHEMA = 'nexloop-sim-credentials/1'
QUEUE = 'eios:action:NexLoop.queue.operations:1'
USES = ('api', 'worker', 'agent', 'twin')
VALID_FROM = '2026-10-21T00:00:00+00:00'
KEYS = {'schema', 'tenant_id', 'world', 'uses', 'token_dir', 'configurator_database_url_file', 'signing_key_file', 'signing_key_id',
        'credential_expires_at', 'check_database_url_file'}
OPTIONAL = {'compose_secret_dir'}
_WORLD = re.compile(r'(sim|shadow):([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})')
_TENANT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
NAMESPACE = uuid.UUID('7a2f2b8e-41c6-4d0a-9a8e-5c1f3e2d1b40')


class Refused(ValueError):
    pass


def read_private(path, maximum=65536):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except (OSError, TypeError):
        raise Refused('private file unreadable: ' + Path(str(path)).name) from None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise Refused('private file must be a regular 0600 file of the caller: ' + Path(str(path)).name)
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise Refused('private file too large')
    return data.decode('utf-8').strip()


def load_config(text):
    try:
        value = json.loads(text)
    except ValueError:
        raise Refused('configuration is not JSON') from None
    if type(value) is not dict or value.get('schema') != SCHEMA or not KEYS <= set(value) <= KEYS | OPTIONAL:
        raise Refused('configuration fields: ' + ', '.join(sorted((KEYS | OPTIONAL) - {'schema'})))
    if type(value['tenant_id']) is not str or not _TENANT.fullmatch(value['tenant_id']):
        raise Refused('tenant_id invalid')
    if type(value['world']) is not str or not _WORLD.fullmatch(value['world']):
        raise Refused('world: sim:<uuid> or shadow:<uuid>')
    uses = value['uses']
    if type(uses) is not list or not uses or len(set(uses)) != len(uses) or any(u not in USES for u in uses):
        raise Refused('uses: a list of api, worker, agent, twin (each once)')
    try:
        expires = datetime.fromisoformat(value['credential_expires_at'])
    except (TypeError, ValueError):
        raise Refused('credential_expires_at: an ISO time') from None
    if expires.tzinfo is None or expires <= datetime.now(expires.tzinfo):
        raise Refused('credential_expires_at: a future time with its zone')
    return {**value, 'credential_expires_at': expires}


def accepted_uses():
    """The uses this release's database accepts (trusted_configuration mirrors the CHECK of authz.nexloop_service_credentials)."""
    from nexloop_eios import trusted_configuration as T
    text = Path(T.__file__).read_text()
    found = re.search(r"sim_use'\) not in \(([^)]*)\)", text)
    return {u.strip().strip("'") for u in found.group(1).split(',')} if found else {'api', 'worker', 'agent'}


def grants_of(use):
    from nexloop_eios.effect_contexts import EFFECT
    effect = 'eios:action:' + EFFECT + ':1'
    return {'api': [QUEUE, effect], 'worker': [QUEUE], 'agent': [effect], 'twin': []}[use]


def principals(config):
    """service_grants principal shapes and the world manifest compile_principal reads (world, valid_from, grants)."""
    slug = _WORLD.fullmatch(config['world']).group(2)[:8]
    out, grants = [], []
    for use in config['uses']:
        role = f'sim_{use}_{slug}'
        name = f'nexloop-sim-{slug}-{use}'
        out.append({'role': role, 'use': use, 'subject_id': name, 'principal_id': name + '-principal', 'credential_id': name + '-key',
                    'credential_reference': role, 'application_id': f'eios:application:{name}',
                    # A binding names 1-64 requested scopes; this label is checked by no entry and matched by no grant (the twin use has none).
                    'requested_scopes': ['nexloop.simulation']})
        grants += [{'principal': role, 'resource_type': 'action', 'resource_id': rid, 'operations': ['execute']} for rid in grants_of(use)]
    return out, {'world': config['world'], 'valid_from': VALID_FROM, 'principals': out, 'grants': grants}


def token_paths(config):
    base = Path(config['token_dir']) / _WORLD.fullmatch(config['world']).group(2)
    return {use: base / f'{use}-credential' for use in config['uses']}


def write_private(path, text):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as out:
        out.write(text)


def tokens(config, *, create):
    out = {}
    for use, path in token_paths(config).items():
        if path.exists() or path.is_symlink():
            out[use] = read_private(path, 4096)
        elif create:
            out[use] = secrets.token_urlsafe(48)
            write_private(path, out[use])
        else:
            out[use] = None
    present = [t for t in out.values() if t]
    if len(set(present)) != len(present):
        raise Refused('every use must hold its own token')
    return out


def plan(config):
    from nexloop_eios import service_grants as G
    people, world_manifest = principals(config)
    current = G.inventory(config['configurator_database_url_file'], config['tenant_id'])
    result = G.plan(world_manifest, config['tenant_id'], current, credential_expires_at=config['credential_expires_at'])
    blocking = [f for f in result['findings'] if f['state'] in ('credential_rotation_required', 'world_mismatch')]
    return people, world_manifest, current, result, blocking


def issue(config, tokens_by_use):
    from nexloop_eios.trusted_configuration import ConfigurationRejected, apply_manifest_with_secrets
    from nexloop_eios.postgres_artifacts import canonical_payload
    people, world_manifest, current, result, blocking = plan(config)
    if blocking:
        raise Refused('existing credentials differ: ' + ', '.join(sorted({f['state'] for f in blocking})))
    by_role = {p['role']: p for p in people}
    if not result['writes'] and not result['new_credentials']:
        return {'changed': False, 'credentials_created': [], 'facts_written': 0}
    body = {'schema_version': '1.0', 'tenant_id': config['tenant_id'], 'expected_revision': current['authority_revision'],
            'tenant_status': current['tenant_status'] or 'active', 'object_types': [], 'actions': [], 'functions': [],
            'authority_facts': [{'kind': k, 'key': list(key), 'payload': f.model_dump(mode='json')} for k, key, f in result['writes']],
            'service_credentials': [{'reference': p['credential_reference'], 'binding': b.model_dump(mode='json'), 'worlds': [config['world']],
                                     'expires_at': expiry.isoformat(), 'status': 'active', 'sim_use': by_role[p['role']]['use']}
                                    for p, b, expiry in result['new_credentials']],
            'browser_applications': [], 'browser_business_applications': [], 'browser_rate_policies': [], 'identity_allowances': []}
    body['manifest_id'] = str(uuid.uuid5(NAMESPACE, config['tenant_id'] + ':' + canonical_payload(body)))
    secrets_map = {p['credential_reference']: tokens_by_use[by_role[p['role']]['use']] for p, _, _ in result['new_credentials']}
    try:
        apply_manifest_with_secrets(body, database_url_file=config['configurator_database_url_file'], signing_key_file=config['signing_key_file'],
                                    signing_key_id=config['signing_key_id'], service_secrets=secrets_map)
    except ConfigurationRejected:
        raise RuntimeError('configuration_rejected') from None
    return {'changed': True, 'credentials_created': sorted(by_role[p['role']]['use'] for p, _, _ in result['new_credentials']),
            'facts_written': len(result['writes'])}


def self_check(config, tokens_by_use, connect=None):
    import psycopg
    connect = connect or (lambda: psycopg.connect(read_private(config['check_database_url_file'], 16384), connect_timeout=5, autocommit=True))
    out, ok = {}, True
    with connect() as db:
        for use, token in sorted(tokens_by_use.items()):
            digest = hashlib.sha256(token.encode()).hexdigest()
            row = {}
            try:
                snap = db.execute('select authz.nexloop_service_identity_snapshot(%s,%s)', (digest, config['world'])).fetchone()[0]
                row['own_world'] = (snap.get('binding') or {}).get('tenant_id') == config['tenant_id']
            except psycopg.Error:
                row['own_world'] = False
            for other in ('real', 'test'):
                try:
                    db.execute('select authz.nexloop_service_identity_snapshot(%s,%s)', (digest, other))
                    row[f'refused_in_{other}'] = False
                except psycopg.Error:
                    row[f'refused_in_{other}'] = True
            out[use] = row
            ok = ok and all(row.values())
    return {'pass': ok, 'credentials': out}


def main(argv=None, *, out=sys.stdout, check_connect=None):
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
    try:
        config = load_config(read_private(args.config))
        unsupported = sorted(set(config['uses']) - accepted_uses())
        if unsupported:
            raise Refused('this release does not accept the use(s) ' + ', '.join(unsupported) + ' (the twin use comes with the eighth batch)')
        if args.dry_run:
            people, _, _, result, blocking = plan(config)
            by_role = {p['role']: p['use'] for p in people}
            return emit({'ok': not blocking, 'dry_run': True, 'world': config['world'],
                         'credentials_to_create': sorted(by_role[p['role']] for p, _, _ in result['new_credentials']),
                         'facts_to_write': len(result['writes']), 'findings': sorted({f['state'] for f in result['findings']}),
                         'tokens_present': {u: p.exists() for u, p in token_paths(config).items()}}, 0 if not blocking else 1)
        if args.check:
            have = tokens(config, create=False)
            if not all(have.values()):
                raise Refused('token files missing for ' + ', '.join(u for u, t in have.items() if not t))
            verdict = self_check(config, have, check_connect)
            return emit({'ok': verdict['pass'], 'check': verdict}, 0 if verdict['pass'] else 1)
        have = tokens(config, create=True)
        report = issue(config, have)
        if config.get('compose_secret_dir') and 'worker' in have:
            target = Path(config['compose_secret_dir']) / 'sim_world_credential'
            if target.exists():
                if read_private(target, 4096) != have['worker']:
                    raise Refused('compose sim_world_credential holds another token (remove it first)')
            else:
                write_private(target, have['worker'])
            report['compose_sim_world_credential'] = 'worker'
        verdict = self_check(config, have, check_connect)
        return emit({'ok': verdict['pass'], 'world': config['world'], **report, 'check': verdict}, 0 if verdict['pass'] else 1)
    except Refused as error:
        return emit({'ok': False, 'detail': str(error)}, 2)
    except Exception as error:  # noqa: BLE001 - the type only (a message may hold a value)
        return emit({'ok': False, 'error': type(error).__name__}, 1)


if __name__ == '__main__':
    sys.exit(main())
