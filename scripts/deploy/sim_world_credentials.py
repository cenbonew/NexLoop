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
    sim_world_credentials.py --config PRIVATE.json --export     the tokens as '<use> <token>' lines on stdout: only as the DATA_HOST end of
                                                                scripts/deploy/sim_credentials_transfer.sh (the dispatcher's machine:
                                                                ssh DATA_HOST --export | ssh APP_HOST --install, one pipeline), which
                                                                sets NEXLOOP_SIM_TRANSFER_PIPE=1; refused otherwise
    sim_world_credentials.py --config PRIVATE.json --forget     removes DATA_HOST's token files once the install is confirmed
    sim_world_credentials.py --install INSTALL.json             reads those lines on stdin and writes the token files and the compose
                                                                sim_world_credential (APP_HOST sim-setup container); no database

Where it runs (L6 review B2.1): issuing, --check and --export run on DATA_HOST in a one-off container on the PostgreSQL socket
directory (deploy/stage/compose.sim-issue.example.yaml) - the configurator and the bootstrap inspection role have local pg_hba rules
only, so both DSN files must name a socket directory (host=/...); a TCP DSN is refused before any connection. --install runs on
APP_HOST (compose.simulation.example.yaml, sim-setup) as the simulation uid and connects to nothing. The tokens move only through the
pipeline sim_credentials_transfer.sh runs on the dispatcher's machine (ssh DATA_HOST ... --export | ssh APP_HOST ... --install): never
argv, a terminal, a log or an intermediate file; the two hosts never trust each other (L6 at046-split review 2.2).

Self-check (check_database_url_file: the bootstrap window's inspection DSN, read only - it reads the credential rows and grants facts
and calls the identity snapshot; never a configurator or runtime DSN): per use exactly one active credential, worlds exactly [world],
sim_use that use, its principal's grant set equal to the minimal list, no other credential of the world; it authenticates in its world
and is refused in real and test with SQLSTATE 42501, and an active real credential of the tenant is accepted in real (positive control).

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
INSTALL_SCHEMA = 'nexloop-sim-credentials-install/1'
INSTALL_KEYS = {'schema', 'world', 'uses', 'token_dir', 'compose_secret_dir'}
_TOKEN = re.compile(r'[A-Za-z0-9_-]{64}')
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


# The fields each mode reads (stage finding 2026-10-22: --export read the configurator DSN file, which exists only inside the
# issuing run's window): a mode requires and checks only these; the others may be present (one configuration for every mode).
NEEDS = {'issue': KEYS, 'dry_run': KEYS,
         'check': {'schema', 'tenant_id', 'world', 'uses', 'token_dir', 'check_database_url_file'},
         'export': {'schema', 'world', 'uses', 'token_dir'}, 'forget': {'schema', 'world', 'uses', 'token_dir'}}


def load_config(text, mode='issue'):
    try:
        value = json.loads(text)
    except ValueError:
        raise Refused('configuration is not JSON') from None
    need = NEEDS[mode]
    if type(value) is not dict or value.get('schema') != SCHEMA or not need <= set(value) <= KEYS | OPTIONAL:
        raise Refused(f'configuration fields ({mode}): ' + ', '.join(sorted(need - {'schema'})) + ' (others allowed: '
                      + ', '.join(sorted((KEYS | OPTIONAL) - need)) + ')')
    if 'tenant_id' in need and (type(value['tenant_id']) is not str or not _TENANT.fullmatch(value['tenant_id'])):
        raise Refused('tenant_id invalid')
    if type(value['world']) is not str or not _WORLD.fullmatch(value['world']):
        raise Refused('world: sim:<uuid> or shadow:<uuid>')
    # L6 batch-12 review (optional): the token directory is an absolute path without parent steps (it names where tokens are written).
    if type(value.get('token_dir')) is not str or not value['token_dir'].startswith('/') or '..' in Path(value['token_dir']).parts:
        raise Refused('token_dir: an absolute path')
    uses = value['uses']
    if type(uses) is not list or not uses or len(set(uses)) != len(uses) or any(u not in USES for u in uses):
        raise Refused('uses: a list of api, worker, agent, twin (each once)')
    if 'credential_expires_at' not in need:
        return value
    try:
        expires = datetime.fromisoformat(value['credential_expires_at'])
    except (TypeError, ValueError):
        raise Refused('credential_expires_at: an ISO time') from None
    if expires.tzinfo is None or expires <= datetime.now(expires.tzinfo):
        raise Refused('credential_expires_at: a future time with its zone')
    return {**value, 'credential_expires_at': expires}


def socket_dsn(path):
    """The DSN in path, refused unless it names a socket directory (host=/...) and no network address (review B2.1)."""
    from psycopg.conninfo import conninfo_to_dict
    text = read_private(path, 16384)
    try:
        parts = conninfo_to_dict(text)
    except Exception:  # noqa: BLE001 - never echo a DSN
        raise Refused('DSN unreadable: ' + Path(str(path)).name) from None
    if not str(parts.get('host') or '').startswith('/') or parts.get('hostaddr') or ',' in str(parts.get('host')):
        raise Refused('DSN must use the local PostgreSQL socket (host=/...): ' + Path(str(path)).name)
    return text


def load_install(text):
    try:
        value = json.loads(text)
    except ValueError:
        raise Refused('configuration is not JSON') from None
    if type(value) is not dict or value.get('schema') != INSTALL_SCHEMA or set(value) != INSTALL_KEYS:
        raise Refused('install configuration fields: ' + ', '.join(sorted(INSTALL_KEYS - {'schema'})))
    if type(value['world']) is not str or not _WORLD.fullmatch(value['world']):
        raise Refused('world: sim:<uuid> or shadow:<uuid>')
    # L6 batch-12 review (optional): the token directory is an absolute path without parent steps (it names where tokens are written).
    if type(value.get('token_dir')) is not str or not value['token_dir'].startswith('/') or '..' in Path(value['token_dir']).parts:
        raise Refused('token_dir: an absolute path')
    uses = value['uses']
    if type(uses) is not list or not uses or len(set(uses)) != len(uses) or any(u not in USES for u in uses):
        raise Refused('uses: a list of api, worker, agent, twin (each once)')
    return value


def read_token_lines(stream, uses):
    got = {}
    for line in stream.read(65536).splitlines():
        if not line.strip():
            continue
        use, _, token = line.strip().partition(' ')
        if use not in uses or use in got or not _TOKEN.fullmatch(token):
            raise Refused('token lines: one "<use> <token>" line per configured use')
        got[use] = token
    if set(got) != set(uses):
        raise Refused('token lines missing for ' + ', '.join(sorted(set(uses) - set(got))))
    return got


def install(config, got):
    """Write the token files (created once; an existing one must hold the same token) and the compose sim_world_credential."""
    written = []
    for use, path in token_paths(config).items():
        if path.exists() or path.is_symlink():
            if read_private(path, 4096) != got[use]:
                raise Refused(f'{use}: the token file holds another token (remove it first)')
        else:
            write_private(path, got[use])
            written.append(use)
    if 'worker' in got:
        target = Path(config['compose_secret_dir']) / 'sim_world_credential'
        if target.exists() or target.is_symlink():
            if read_private(target, 4096) != got['worker']:
                raise Refused('compose sim_world_credential holds another token (remove it first)')
        else:
            write_private(target, got['worker'])
    return {'ok': True, 'installed': sorted(written), 'compose_sim_world_credential': 'worker' in got}


def accepted_uses():
    """The uses this release's database accepts (trusted_configuration mirrors the CHECK of authz.nexloop_service_credentials)."""
    from nexloop_eios import trusted_configuration as T
    text = Path(T.__file__).read_text()
    found = re.search(r"sim_use'\) not in \(([^)]*)\)", text)
    return {u.strip().strip("'") for u in found.group(1).split(',')} if found else {'api', 'worker', 'agent'}


def grants_of(use):
    # The effect Action is granted to a simulation world's principals only (review 4.2: the one exception to "no effect Action through a
    # manifest"); principals() asserts the world is sim:/shadow: before any grant is built, and the database binds the credential to it.
    from nexloop_eios.effect_contexts import EFFECT
    effect = 'eios:action:' + EFFECT + ':1'
    return {'api': [QUEUE, effect], 'worker': [QUEUE], 'agent': [effect], 'twin': []}[use]


def principals(config):
    """service_grants principal shapes and the world manifest compile_principal reads (world, valid_from, grants)."""
    found = _WORLD.fullmatch(config['world'])
    if not found or found.group(1) not in ('sim', 'shadow'):  # review 4.2: never an effect grant outside a simulation or shadow world
        raise Refused('world: sim:<uuid> or shadow:<uuid>')
    slug = found.group(2)[:8]
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
    socket_dsn(config['configurator_database_url_file'])  # review B2.1: the configurator over the DATA_HOST socket only
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
    """Review 4.3: every item, each one a named result.
    Rows (authz.nexloop_service_credentials of the tenant naming this world, and the world principals' grants facts):
      exactly one active credential per configured use and no other credential of the world; for each, worlds is exactly [world],
      sim_use is that use, status active, its principal's grant set equals grants_of(use) (execute only).
    Behaviour: the token authenticates in its world (identity snapshot, binding tenant); in real and test it is refused with SQLSTATE
      42501 (any other error is not a refusal); positive control: an active real credential of the tenant is accepted in real, so the
      refusals are the world binding's and not a broken call."""
    import psycopg
    dsn = socket_dsn(config['check_database_url_file'])  # review B2.1: the bootstrap inspection role over the DATA_HOST socket only
    connect = connect or (lambda: psycopg.connect(dsn, connect_timeout=5, autocommit=True))
    people, _ = principals(config)
    by_credential = {p['credential_id']: p for p in people}
    out, ok = {}, True
    with connect() as db:
        db.execute("select set_config('eios.tenant_id',%s,false)", (config['tenant_id'],))
        rows = db.execute("""select token_digest,credential_id,worlds,sim_use,status from authz.nexloop_service_credentials
            where tenant_id=%s and %s=any(worlds)""", (config['tenant_id'], config['world'])).fetchall()
        facts = db.execute("""select entity_key[1],payload->'grants' from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='grants'""",
                           (config['tenant_id'],)).fetchall()
        grants = {}
        for principal, items in facts:
            grants.setdefault(principal, set()).update((g['resource']['resource_id'], tuple(sorted(g['operations']))) for g in items or [])
        active = [r for r in rows if r[4] == 'active']
        uses_found = sorted(r[3] or '' for r in active)
        out['_world'] = {'one_active_per_use': uses_found == sorted(tokens_by_use), 'no_other_credential': all(r[1] in by_credential for r in rows)}
        ok = all(out['_world'].values())

        def snapshot(digest, world):
            try:
                with db.transaction():
                    return db.execute('select authz.nexloop_service_identity_snapshot(%s,%s)', (digest, world)).fetchone()[0], None
            except psycopg.Error as error:
                return None, error.sqlstate

        for use, token in sorted(tokens_by_use.items()):
            digest = hashlib.sha256(token.encode()).hexdigest()
            mine = [r for r in active if r[0] == digest]
            row = mine[0] if len(mine) == 1 else None
            person = by_credential.get(row[1]) if row else None
            item = {'row_found': row is not None,
                    'worlds_exactly_this_world': bool(row) and list(row[2]) == [config['world']],
                    'sim_use': bool(row) and row[3] == use and bool(person) and person['use'] == use,
                    'status_active': bool(row) and row[4] == 'active',
                    'grants_minimal': bool(person) and grants.get(person['principal_id'], set()) == {(rid, ('execute',)) for rid in grants_of(use)}}
            snap, _ = snapshot(digest, config['world'])
            item['own_world'] = bool(snap) and (snap.get('binding') or {}).get('tenant_id') == config['tenant_id']
            for other in ('real', 'test'):
                snap, state = snapshot(digest, other)
                item[f'refused_in_{other}'] = snap is None and state == '42501'
            out[use] = item
            ok = ok and all(item.values())
        control = db.execute("""select token_digest from authz.nexloop_service_credentials where tenant_id=%s and worlds=array['real'] and status='active'
            and (expires_at is null or expires_at>clock_timestamp()) order by credential_id limit 1""", (config['tenant_id'],)).fetchone()
        accepted = snapshot(control[0], 'real')[0] if control else None
        out['_control'] = {'real_credential_accepted_in_real': bool(accepted)}
        ok = ok and bool(accepted)
    return {'pass': ok, 'credentials': out}


def main(argv=None, *, out=sys.stdout, check_connect=None, stdin=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.error = lambda message: (_ for _ in ()).throw(SystemExit(2))  # never echo the arguments
    parser.add_argument('--config')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--export', action='store_true')
    mode.add_argument('--forget', action='store_true')
    mode.add_argument('--install')
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2

    def emit(value, code):
        print(json.dumps(value, sort_keys=True), file=out)
        return code
    if bool(args.config) == bool(args.install):
        return 2
    try:
        if args.install:
            config = load_install(read_private(args.install))
            return emit(install(config, read_token_lines(stdin or sys.stdin, config['uses'])), 0)
        mode = 'dry_run' if args.dry_run else 'export' if args.export else 'forget' if args.forget else 'check' if args.check else 'issue'
        config = load_config(read_private(args.config), mode)
        if mode in ('issue', 'dry_run'):  # review B2.1: refused before any token file or connection
            socket_dsn(config['configurator_database_url_file'])
        if mode in ('issue', 'check'):
            socket_dsn(config['check_database_url_file'])
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
        if args.export:
            # L6 at046-split review 2.1: inside `docker compose run -T` the stdout is always a pipe, so this tool cannot tell where its
            # output goes. --export runs only under scripts/deploy/sim_credentials_transfer.sh (the dispatcher's machine: one pipeline,
            # ssh DATA_HOST into ssh APP_HOST --install), which alone sets this marker - a guard against running it by hand, not a
            # security boundary.
            if os.environ.get('NEXLOOP_SIM_TRANSFER_PIPE') != '1':
                raise Refused('--export runs only inside scripts/deploy/sim_credentials_transfer.sh (one pipeline into --install)')
            have = tokens(config, create=False)
            if not all(have.values()):
                raise Refused('token files missing for ' + ', '.join(u for u, t in have.items() if not t))
            out.write(''.join(f'{u} {t}\n' for u, t in sorted(have.items())))
            return 0
        if args.forget:
            # Review 2.1: after the install on APP_HOST, DATA_HOST keeps no copy (a later --check or --export needs a new issue).
            removed = []
            for use, path in token_paths(config).items():
                if path.is_symlink() or (path.exists() and not path.is_file()):
                    raise Refused(f'{use}: the token path is not a regular file')
                if path.exists():
                    path.unlink()
                    removed.append(use)
            return emit({'ok': True, 'forgotten': sorted(removed)}, 0)
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
