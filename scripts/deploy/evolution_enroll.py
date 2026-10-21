#!/usr/bin/env python3
"""Stage enrollment of the evolution service principals (NX-038/NX-039; owner decision 2026-10-21 "as recommended": register the
evolution writer and evaluator on stage for the S6 stage acceptance). Owner rule: a private configuration file and one command.

Three service principals, each with its own token and NO grant at all (AT-046: no effect authority; the evolution entries check
the credential in SQL, they need no EIOS grant):
  evolution_writer      world real   authz.nexloop_evo_principals purpose writer     login role nexloop_evo_writer (0235)
  evolution_evaluator   world real   authz.nexloop_evo_principals purpose evaluator  login role nexloop_evo_evaluator (0241)
  evolution_evaluation  world test   authz.nexloop_evo_evaluation_credentials (D18)  used by the L1 session (0248) and the overlay (0237)
One purpose per principal (S6-R5 / D39-1): the writer and the evaluator are different principals with different tokens, and the
test-only evaluation credential is neither of them.

Steps (each idempotent; a second run changes nothing):
  1. tokens      <secret_dir>/evolution-writer/service-credential, evolution-evaluator/service-credential and
                 evolution-evaluation/test-credential (0600, created once, never printed; an existing file is reused as it is).
  2. credentials the subject, membership, actor, authentication and application facts and the service credential of each principal
                 through the trusted configuration (0050 configurator definer, nexloop_eios.service_grants.plan/apply; world real for
                 writer and evaluator, world test for the evaluation credential). No grant, scope or policy fact is written.
  3. registry    authz.nexloop_evo_principals (writer, evaluator) and authz.nexloop_evo_evaluation_credentials (the test credential).
                 These tables are revoked from every application and configurator role by design, so this step needs the owner's
                 bootstrap window (a superuser or nexloop_owner session: registry_database_url_file).
  4. self-check  writer/evaluator exclusive, the evaluation credential test-only, no principal holding any grant (no effect
                 authority); the two login roles exist. A failed check exits 1.

    evolution_enroll.py --config PRIVATE.json --dry-run     reads the database, writes nothing, prints the plan
    evolution_enroll.py --config PRIVATE.json               enrolls (then self-checks)
    evolution_enroll.py --config PRIVATE.json --check       self-check only

Configuration (private JSON, 0600, owned by the caller; deploy/examples/evolution-enroll.example.json):
  {"schema": "nexloop-evolution-enroll/1", "tenant_id": ..., "secret_dir": ..., "configurator_database_url_file": ...,
   "registry_database_url_file": ..., "signing_key_file": ..., "signing_key_id": ..., "credential_expires_at": "<ISO, UTC>",
   "registered_by": "<who registers, e.g. owner-config:2026-10-21>"}
The login roles' passwords (bootstrap window) and the pg_hba line (template + scripts/deploy/evolution_hba_data_host.sh) are not
set here: the output names them (`operator_sheet`). Output: one JSON object; never a token, a password or a DSN. Exit 0 done, 1 self-check failed or refused by
the database, 2 input refused.
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'packages/eios-core/src'))

SCHEMA = 'nexloop-evolution-enroll/1'
APPLICATION = 'eios:application:nexloop-evolution'
# Fixed so the compiled facts are deterministic (re-applying writes nothing); in the past of every credential.
VALID_FROM = '2026-10-21T00:00:00+00:00'
PRINCIPALS = (
    {'role': 'evolution_writer', 'world': 'real', 'purpose': 'writer', 'login_role': 'nexloop_evo_writer',
     'file': 'evolution-writer/service-credential'},
    {'role': 'evolution_evaluator', 'world': 'real', 'purpose': 'evaluator', 'login_role': 'nexloop_evo_evaluator',
     'file': 'evolution-evaluator/service-credential'},
    {'role': 'evolution_evaluation', 'world': 'test', 'purpose': 'evaluation_credential', 'login_role': None,
     'file': 'evolution-evaluation/test-credential'},
)
LOGIN_ROLES = ('nexloop_evo_writer', 'nexloop_evo_evaluator')
# A credential binding names 1-64 requested scopes (eios.authz.facts). This one is checked by no entry and matched by no scope
# authority fact (those are written per grant, and there is none), so it grants nothing; it only labels the credential.
LABEL_SCOPE = 'nexloop.evolution'
KEYS = {'schema', 'tenant_id', 'secret_dir', 'configurator_database_url_file', 'registry_database_url_file', 'signing_key_file',
        'signing_key_id', 'credential_expires_at', 'registered_by'}
_TENANT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
_KEY_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,159}')


class Refused(ValueError):
    """Input refused (exit 2)."""


def identifiers(role):
    slug = role.replace('_', '-')
    return {'subject_id': f'nexloop-service-{slug}', 'principal_id': f'nexloop-service-{slug}-principal',
            'credential_id': f'nexloop-service-{slug}-key', 'credential_reference': role}


def manifest_principal(p):
    """The service_grants principal shape (database_role is descriptive here: the evolution login roles are not application roles)."""
    # One application per principal: its version digest names the principal and the world, so a shared one would be rewritten
    # by every other principal's facts (and a rerun would never be a no-op).
    return {'role': p['role'], **identifiers(p['role']), 'application_id': f"{APPLICATION}-{p['role'].replace('_', '-')}", 'database_role': p['login_role'] or 'nexloop_api',
            'purpose': f"NX-038/NX-039 evolution {p['purpose']} (stage enrollment, no grant)", 'source_task': 'NX-039 S6 stage',
            'requested_scopes': [LABEL_SCOPE]}


def manifest(world):
    return {'schema_version': 'nexloop-service-grants/1', 'manifest_version': 1, 'decision': 'evolution stage enrollment', 'valid_from': VALID_FROM,
            'world': world, 'principals': [manifest_principal(p) for p in PRINCIPALS if p['world'] == world], 'grants': [],
            'deferred': [], 'excluded_by_policy': []}


def read_private(path, maximum=65536):
    from nexloop_eios.private_configuration import read_private_text
    try:
        return read_private_text(path, maximum=maximum)
    except (OSError, ValueError):
        raise Refused('private file unreadable or not a 0600 file of the caller: ' + Path(path).name) from None


def load_config(text):
    try:
        value = json.loads(text)
    except ValueError:
        raise Refused('configuration is not JSON') from None
    if type(value) is not dict or value.get('schema') != SCHEMA:
        raise Refused('configuration schema: ' + SCHEMA)
    if set(value) != KEYS or not all(type(value[k]) is str and value[k] for k in KEYS):
        raise Refused('configuration fields: ' + ', '.join(sorted(KEYS - {'schema'})))
    if not _TENANT.fullmatch(value['tenant_id']) or not _KEY_ID.fullmatch(value['signing_key_id']) or not 1 <= len(value['registered_by']) <= 200:
        raise Refused('tenant_id, signing_key_id or registered_by invalid')
    try:
        expires = datetime.fromisoformat(value['credential_expires_at'])
    except ValueError:
        raise Refused('credential_expires_at: an ISO time') from None
    if expires.tzinfo is None or expires.utcoffset().total_seconds() != 0 or expires <= datetime.now(expires.tzinfo):
        raise Refused('credential_expires_at: a future UTC time')
    return {**value, 'credential_expires_at': expires}


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _write_private(path, text):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as out:
        out.write(text)


def tokens(secret_dir, *, create):
    """role -> token (None when absent and not created). An existing file is reused; a new one holds 48 random bytes (url-safe)."""
    out, created = {}, []
    for p in PRINCIPALS:
        path = Path(secret_dir) / p['file']
        if path.exists() or path.is_symlink():
            token = read_private(path, 4096)
            if len(token) < 32:
                raise Refused('credential file too short: ' + p['file'])
        elif create:
            token = secrets.token_urlsafe(48)
            _write_private(path, token)
            created.append(p['file'])
        else:
            token = None
        out[p['role']] = token
    if len({t for t in out.values() if t is not None}) != len([t for t in out.values() if t is not None]):
        raise Refused('the evolution principals must hold different tokens')
    return out, created


def _apply_credentials(config, world, tokens_by_role, *, dry_run):
    from nexloop_eios import service_grants as G
    m = manifest(world)
    if dry_run:
        current = G.inventory(config['configurator_database_url_file'], config['tenant_id'])
        result = G.plan(m, config['tenant_id'], current, credential_expires_at=config['credential_expires_at'])
        return {'world': world, 'credentials_to_create': sorted(p['credential_reference'] for p, _, _ in result['new_credentials']),
                'facts_to_write': len(result['writes']), 'findings': result['findings']}
    scratch = Path(tempfile.mkdtemp(prefix='nexloop-evo-enroll-'))
    try:
        staged = scratch / 'secrets.json'
        _write_private(staged, json.dumps({p['role']: tokens_by_role[p['role']] for p in PRINCIPALS if p['world'] == world}))
        report = G.apply(m, config['tenant_id'], database_url_file=config['configurator_database_url_file'],
                         signing_key_file=config['signing_key_file'], signing_key_id=config['signing_key_id'], service_secrets_file=staged,
                         credential_expires_at=config['credential_expires_at'])
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return {'world': world, 'changed': report['changed'], 'facts_written': sorted({f['kind'] for f in report['facts_written']}), 'credentials_created': sorted(report['credentials_created']),
            'grants_added': len(report['grants_added']), 'grants_revoked': len(report['grants_revoked'])}


def _registry_connection(config):
    import psycopg
    db = psycopg.connect(read_private(config['registry_database_url_file'], 16384), connect_timeout=5, autocommit=False)
    allowed = db.execute("select rolsuper or current_user='nexloop_owner' from pg_roles where rolname=current_user").fetchone()
    db.rollback()
    if not allowed or not allowed[0]:
        db.close()
        raise Refused('registry_database_url_file: the bootstrap window (superuser or nexloop_owner) registers evolution principals')
    return db


def _principal_rows(db, ids):
    return {r[0]: (r[1], r[2]) for r in db.execute('select principal_id,tenant_id,purpose from authz.nexloop_evo_principals where principal_id = any(%s)',
                                                   (list(ids),)).fetchall()}


def register(config, tokens_by_role, *, dry_run):
    """authz.nexloop_evo_principals (writer, evaluator) and authz.nexloop_evo_evaluation_credentials (the test-only credential)."""
    tenant = config['tenant_id']
    planned, written = [], []
    with _registry_connection(config) as db, db.transaction():
        db.execute("set local statement_timeout='10000ms'; set local lock_timeout='3000ms'")
        rows = _principal_rows(db, [identifiers(p['role'])['principal_id'] for p in PRINCIPALS])
        for p in PRINCIPALS:
            if p['purpose'] not in ('writer', 'evaluator'):
                continue
            pid = identifiers(p['role'])['principal_id']
            if pid in rows:
                if rows[pid] != (tenant, p['purpose']):
                    raise Refused(f"{p['role']} is registered for another tenant or purpose (never changed)")
                continue
            planned.append(f"principal:{p['role']}:{p['purpose']}")
            if not dry_run:
                db.execute('insert into authz.nexloop_evo_principals(principal_id,tenant_id,purpose,registered_by) values(%s,%s,%s,%s)',
                           (pid, tenant, p['purpose'], config['registered_by']))
                written.append(planned[-1])
        token = tokens_by_role.get('evolution_evaluation')
        known = token is not None and db.execute('select 1 from authz.nexloop_evo_evaluation_credentials where token_digest=%s', (digest(token),)).fetchone()
        if not known:
            planned.append('evaluation_credential:evolution_evaluation')
            if not dry_run:
                db.execute('insert into authz.nexloop_evo_evaluation_credentials(token_digest,tenant_id,registered_by) values(%s,%s,%s)',
                           (digest(token), tenant, config['registered_by']))
                written.append(planned[-1])
    return {'planned': planned, 'written': written}


def self_check(config, tokens_by_role):
    """The three safety properties, read back from the database (superuser/owner session), plus the login roles."""
    tenant = config['tenant_id']
    ids = {p['role']: identifiers(p['role'])['principal_id'] for p in PRINCIPALS}
    digests = {r: (digest(t) if t else None) for r, t in tokens_by_role.items()}
    with _registry_connection(config) as db:
        rows = _principal_rows(db, ids.values())
        evaluation = {r[0] for r in db.execute('select token_digest from authz.nexloop_evo_evaluation_credentials where tenant_id=%s', (tenant,)).fetchall()}
        creds = {r[0]: (r[1], r[2], r[3]) for r in db.execute(
            'select token_digest,worlds,status,binding->>\'subject_principal_id\' from authz.nexloop_service_credentials where token_digest = any(%s)',
            ([d for d in digests.values() if d],)).fetchall()}
        # Any authority fact naming one of the three principals beyond identity: grants, scopes, policies, controls, rules.
        authority = db.execute("""select count(*) from authz.nexloop_authority_facts where tenant_id=%s
            and fact_kind not in ('subject','membership','actor','authentication','application','subject_authority','revision')
            and (payload->>'principal_id' = any(%s) or payload->>'subject_id' = any(%s) or entity_key[1] = any(%s))""",
                               (tenant, list(ids.values()), list(ids.values()), list(ids.values()))).fetchone()[0]
        scopes = db.execute("""select count(*) from authz.nexloop_service_credentials where token_digest = any(%s)
            and coalesce(binding->'requested_scopes','[]'::jsonb) <> %s::jsonb""", ([d for d in digests.values() if d], json.dumps([LABEL_SCOPE]))).fetchone()[0]
        roles = {r[0]: r[1] for r in db.execute('select rolname,rolcanlogin from pg_roles where rolname = any(%s)', (list(LOGIN_ROLES),)).fetchall()}
        db.rollback()
    w, e, t = digests['evolution_writer'], digests['evolution_evaluator'], digests['evolution_evaluation']
    checks = {
        # S6-R5 / D39-1: two principals, one purpose each, different tokens; neither is the test-only evaluation credential.
        'writer_evaluator_exclusive': (rows.get(ids['evolution_writer']) == (tenant, 'writer') and rows.get(ids['evolution_evaluator']) == (tenant, 'evaluator')
                                       and ids['evolution_evaluation'] not in rows and None not in (w, e) and w != e
                                       and w not in evaluation and e not in evaluation
                                       and creds.get(w, (None, None, None))[2] == ids['evolution_writer']
                                       and creds.get(e, (None, None, None))[2] == ids['evolution_evaluator']),
        # D18: the evaluation credential is registered and issued for world test alone; writer and evaluator for real alone.
        'evaluation_credential_test_only': (t is not None and t in evaluation and creds.get(t, (None,))[0] == ['test']
                                            and creds.get(w, (None,))[0] == ['real'] and creds.get(e, (None,))[0] == ['real']
                                            and all(creds.get(x, (None, None))[1] == 'active' for x in (w, e, t))),
        # AT-046: no grant, scope, policy or rule for any of them and only the label scope, so no effect authority (and no Action at all).
        'no_effect_authority': authority == 0 and scopes == 0,
    }
    return {'checks': checks, 'login_roles': {r: bool(roles.get(r)) for r in LOGIN_ROLES}, 'pass': all(checks.values()) and all(roles.get(r) for r in LOGIN_ROLES)}


def operator_sheet(config):
    """What this tool does not do: the login roles' passwords (bootstrap window, Keychain) and pg_hba (host configuration)."""
    return [
        'Set a password for nexloop_evo_writer and nexloop_evo_evaluator in the bootstrap window (generated straight into the Keychain); '
        'write each DSN to its own 0600 file next to the credential files (evolution-writer/database-url, evolution-evaluator/database-url).',
        'pg_hba: deploy/stage/config/pg_hba.conf.template carries the evolution line (local socket, stage catalog, SCRAM); install it on '
        'DATA_HOST with the owner command `sudo bash scripts/deploy/evolution_hba_data_host.sh <private config>` (render, rewrite in place, '
        'reload); run this tool and the acceptance in a one-off DATA_HOST container on the mounted socket directory.',
        'The writer reads ' + PRINCIPALS[0]['file'] + ', the evaluator ' + PRINCIPALS[1]['file'] + ' and ' + PRINCIPALS[2]['file']
        + ' (the L1 session); no long-running service is needed: diagnosis, candidate and evaluation are one-off commands.',
    ]


def main(argv=None, *, out=sys.stdout):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--config', required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--check', action='store_true')
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2

    def emit(value, code):
        print(json.dumps(value, sort_keys=True, ensure_ascii=False), file=out)
        return code
    try:
        config = load_config(read_private(args.config))
        from nexloop_eios.service_grants import ServiceGrantsRejected
        if args.check:
            by_role, _ = tokens(config['secret_dir'], create=False)
            result = self_check(config, by_role)
            return emit({'ok': result['pass'], 'mode': 'check', **result}, 0 if result['pass'] else 1)
        by_role, created = tokens(config['secret_dir'], create=not args.dry_run)
        try:
            credentials = [_apply_credentials(config, world, by_role, dry_run=args.dry_run) for world in ('real', 'test')]
        except ServiceGrantsRejected as error:
            return emit({'ok': False, 'stage': 'credentials', 'refused': error.reason}, 1)
        if args.dry_run:
            if by_role['evolution_evaluation'] is None:
                registry = {'planned': ['principal:evolution_writer:writer', 'principal:evolution_evaluator:evaluator',
                                        'evaluation_credential:evolution_evaluation'], 'written': []}
            else:
                registry = register(config, by_role, dry_run=True)
            return emit({'ok': True, 'mode': 'dry_run', 'tokens_to_create': [p['file'] for p in PRINCIPALS if by_role[p['role']] is None],
                         'credentials': credentials, 'registry': registry, 'operator_sheet': operator_sheet(config)}, 0)
        registry = register(config, by_role, dry_run=False)
        result = self_check(config, by_role)
        changed = bool(created or registry['written'] or any(c['changed'] for c in credentials))
        return emit({'ok': result['pass'], 'mode': 'enroll', 'changed': changed, 'tokens_created': created, 'credentials': credentials,
                     'registry': registry, **result, 'operator_sheet': operator_sheet(config)}, 0 if result['pass'] else 1)
    except Refused as error:
        return emit({'ok': False, 'refused': str(error)}, 2)
    except Exception as error:  # never a DSN, path or token in the output: the class only
        return emit({'ok': False, 'error': type(error).__name__}, 1)


if __name__ == '__main__':
    sys.exit(main())
