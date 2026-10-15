#!/usr/bin/env python3
"""NX-032 A1 design §4.2/§4.3 (T6): the two worker configuration files that depend on a tenant's published state, generated
after publish and tenant assembly, read-only.

    tenant_config.py recall-types --tenant UUID --bootstrap-dsn-file F --out FILE [--types Consumer,...]
    tenant_config.py relay-run-profile --tenant UUID --bootstrap-dsn-file F --out FILE --role-id HEX64
        --maximum-cost DECIMAL --currency CCY [--runtime-profile deepseek-flash] [--maximum-model-turns 8]
        [--maximum-tool-calls 8] [--active-timeout-seconds 120] [--runtime-owner-epoch 1] [--queue operations]

recall-types (A4, the recall-indexer's /etc/nexloop/recall-types.json, read by background_services.load_types): {type name: null}
for each requested type (default Consumer, the minimal set until NX-019/NX-021 decide which types are recalled). null keeps the
type definition's defaults. Every type must be published for the tenant (ontology.object_type_versions); the indexer re-checks it
in SQL at indexing time.

relay-run-profile (A5, /etc/nexloop/relay-run-profile.json of both message relays and the reply guarantor, read by
message_relay.validate_run_profile): the six deployment-level Run settings. What the code does with them:
- role_ref: copied into every relay Run command (message_relay.py, reply_fallback.py) and recorded with its outbound messages; SQL
  does not resolve it for message Runs (only Role Runs bind role:<role>:mapping:<link>). Here it names a published RoleDefinition
  object that is active and currently valid: role:<object id>.
- context_manifest_ref: never used as given; the relay and the fallback build the command with artifact:context-bind-pending and
  replace it with the bound Context artifact before binding (message_relay.py ~320/336, reply_fallback.py ~109/113). A Context
  Manifest is a per-model-call audit record (context_engine/audit.py), not a tenant object, so the profile carries the same
  placeholder the relay uses.
- runtime_profile, budget, runtime_owner_epoch: copied into the Run command (runtime_activation._command bounds them; the owner
  epoch must equal the activation registry's, 0037).
- queue: the Run activation queue; NexLoop.queue.<queue>:1 must be published (runtime_activation reads its definition).
Nothing is written to the database. The output file is new (never overwritten), mode 0600; the operator installs it with the
intended owner. Prints one JSON line without values. Exit 0 written, 2 refused.
"""
import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'packages/eios-core/src'))

CONTEXT_PENDING = 'artifact:context-bind-pending'
_HEX64 = re.compile('[a-f0-9]{64}')


class Refused(ValueError):
    pass


def published_types(connection, tenant):
    return {r[0] for r in connection.execute('select distinct type_name from ontology.object_type_versions where tenant_id=%s', (tenant,))}


def recall_types(published, requested):
    """{type: None} for the requested types, all of which must be published."""
    from nexloop_eios.background_services import _TYPE
    if not requested or len(requested) > 64 or len(set(requested)) != len(requested) or not all(_TYPE.fullmatch(t) for t in requested):
        raise Refused('--types: 1 to 64 distinct type names')
    missing = sorted(set(requested) - set(published))
    if missing:
        raise Refused('types not published for the tenant: ' + ','.join(missing))
    return {t: None for t in sorted(requested)}


def role_state(connection, tenant, role_id):
    row = connection.execute("select properties from ontology.objects where tenant_id=%s and world='real' and type_name='RoleDefinition' and object_id=%s",
                             (tenant, role_id)).fetchone()
    return None if row is None else row[0]


def queue_published(connection, tenant, queue):
    return connection.execute("select active from control.nexloop_action_definitions where tenant_id=%s and world='real' and resource_id=%s",
                              (tenant, f'eios:action:NexLoop.queue.{queue}:1')).fetchone() == (True,)


def run_profile(*, role_id, role, queue_is_published, runtime_profile, budget, runtime_owner_epoch, queue, now=None):
    from nexloop_eios.message_relay import validate_run_profile
    if not isinstance(role_id, str) or not _HEX64.fullmatch(role_id):
        raise Refused('--role-id: the 64-hex object id of a RoleDefinition')
    if role is None:
        raise Refused('RoleDefinition not found in the real world of the tenant')
    now = now or datetime.now(UTC)
    try:
        valid = role.get('active') is True and datetime.fromisoformat(role['valid_from']) <= now < datetime.fromisoformat(role['valid_until'])
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise Refused('RoleDefinition is not active and currently valid')
    if not queue_is_published:
        raise Refused(f'NexLoop.queue.{queue}:1 is not published (active) for the tenant')
    profile = {'role_ref': 'role:' + role_id, 'context_manifest_ref': CONTEXT_PENDING, 'runtime_profile': runtime_profile, 'budget': budget,
               'runtime_owner_epoch': runtime_owner_epoch, 'queue': queue}
    try:
        return validate_run_profile(profile)
    except Exception:
        raise Refused('the run profile does not pass message_relay.validate_run_profile (budget bounds, queue name, owner epoch)') from None


def write_new(path, value):
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(json.dumps(value, indent=1, sort_keys=True) + '\n')
    os.chmod(path, 0o600)


def _read(bootstrap_dsn_file, reader):
    import psycopg
    from nexloop_eios.private_configuration import read_private_text
    with psycopg.connect(read_private_text(bootstrap_dsn_file, maximum=16384).strip(), connect_timeout=5) as connection:
        connection.execute('set transaction read only')
        return reader(connection)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('recall-types', 'relay-run-profile'):
        p = sub.add_parser(name)
        p.add_argument('--tenant', required=True)
        p.add_argument('--bootstrap-dsn-file', type=Path, required=True, help='read-only reads of the published state')
        p.add_argument('--out', type=Path, required=True)
    sub.choices['recall-types'].add_argument('--types', default='Consumer')
    r = sub.choices['relay-run-profile']
    r.add_argument('--role-id', required=True)
    r.add_argument('--runtime-profile', default='deepseek-flash')
    r.add_argument('--maximum-model-turns', type=int, default=8)
    r.add_argument('--maximum-tool-calls', type=int, default=8)
    r.add_argument('--active-timeout-seconds', type=int, default=120)
    r.add_argument('--maximum-cost', required=True)
    r.add_argument('--currency', required=True)
    r.add_argument('--runtime-owner-epoch', type=int, default=1)
    r.add_argument('--queue', default='operations')
    args = parser.parse_args(argv)
    import psycopg
    try:
        if args.out.exists():
            raise Refused('output exists')
        if args.command == 'recall-types':
            value = recall_types(_read(args.bootstrap_dsn_file, lambda c: published_types(c, args.tenant)), args.types.split(','))
            summary = {'types': sorted(value)}
        else:
            role, queue_ok = _read(args.bootstrap_dsn_file, lambda c: (role_state(c, args.tenant, args.role_id),
                                                                       queue_published(c, args.tenant, args.queue)))
            budget = {'maximum_model_turns': args.maximum_model_turns, 'maximum_tool_calls': args.maximum_tool_calls,
                      'active_timeout_seconds': args.active_timeout_seconds, 'maximum_cost': args.maximum_cost, 'currency': args.currency}
            value = run_profile(role_id=args.role_id, role=role, queue_is_published=queue_ok, runtime_profile=args.runtime_profile,
                                budget=budget, runtime_owner_epoch=args.runtime_owner_epoch, queue=args.queue)
            summary = {'queue': value['queue'], 'runtime_profile': value['runtime_profile']}
        write_new(args.out, value)
    except (Refused, OSError, ValueError, psycopg.Error) as error:
        print(json.dumps({'ok': False, 'error': type(error).__name__, 'detail': str(error) if isinstance(error, Refused) else None}))
        return 2
    print(json.dumps({'ok': True, 'command': args.command, **summary}, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
