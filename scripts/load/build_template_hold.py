"""NX-031 slice 0: build the load template (run only through scripts/load/build_template.sh; outside `testpaths`, so CI never
collects it). Disposable PostgreSQL from the test fixtures (bootstrap of the clean catalog), the fixed-seed manifest loaded only
through governed entries (tests/load_template.py), the workbench Actions and members (owner, two operators) by trusted
configuration, then an online pg_basebackup of the loaded cluster. Writes into $NEXLOOP_LOAD_OUT:
  manifest.jsonl, template.json (seed, digest, counts, timings, code commit, catalog revision; no secrets),
  private/credentials.json and private/signing.json (0600; synthetic accounts and the template's signing key),
  pgdata/ (the template cluster).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from conftest import admin, pg  # noqa: F401
from test_action_definitions import published_action  # noqa: F401
from test_browser_business_authorization import browser_business  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_conversation_messages import conversations  # noqa: F401
from commitment_fixture import TAKEOVER_ACTIONS, publish_request_actions
from test_workbench_actions_http import publish_goal_actions
from test_workbench_read_pg import CALLER, ROLES, TENANT, WORKBENCH_APP, add_human, members_file, write_facts
from load_template import load, snapshot

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/load'))
import synthetic  # noqa: E402


def private(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        handle.write(content)
    return path


def test_build_load_template(conversations, admin, pg, tmp_path):
    from nexloop_eios.workbench_roles import apply_members, compile_members
    out = Path(os.environ['NEXLOOP_LOAD_OUT'])
    seed, consumers = int(os.environ['NEXLOOP_LOAD_SEED']), int(os.environ['NEXLOOP_LOAD_CONSUMERS'])
    lines = synthetic.lines(seed, consumers)
    (out / 'manifest.jsonl').write_text('\n'.join(lines) + '\n')
    *records, trailer = [json.loads(x) for x in lines]
    started = time.monotonic()
    result = load(conversations, admin, records,
                  progress=lambda n, s: print(f'loaded {n}/{consumers} consumers in {s:.0f} s', flush=True))
    publish_request_actions(admin)
    publish_request_actions(admin, names=TAKEOVER_ACTIONS)
    publish_goal_actions(admin)
    admin.execute('insert into control.nexloop_browser_applications values(%s,%s,1,true)', (TENANT, WORKBENCH_APP))
    admin.execute('insert into control.nexloop_browser_business_applications values(%s,%s,%s,%s,%s)', (TENANT, WORKBENCH_APP, CALLER, '1', Jsonb(['action.execute'])))
    staff = {'owner': add_human(admin, 'owner'), 'operator-1': add_human(admin, 'operator-1'), 'operator-2': add_human(admin, 'operator-2')}
    admin.execute('alter role nexloop_configurator login')
    dsn = tmp_path / 'configurator-dsn'
    dsn.write_text(make_conninfo(pg, user='nexloop_configurator'))
    dsn.chmod(0o600)
    members = members_file(*((h, 'owner' if k == 'owner' else 'operator') for k, h in staff.items()))
    apply_members(ROLES, members, database_url_file=dsn)
    write_facts(admin, compile_members(ROLES, members))
    load_seconds = round(time.monotonic() - started, 1)
    backup_seconds = snapshot(pg, out)
    reader = conversations['reader']
    (out / 'private').mkdir(mode=0o700)
    private(out / 'private/credentials.json', json.dumps({'customers': result['credentials'],
        'workbench': {k: {'username': h['username'], 'password': h['password'], 'principal_id': h['principal_id']} for k, h in staff.items()}}))
    private(out / 'private/signing.json', json.dumps({'key_id': reader.signer.key_id, 'material_hex': reader.signer.material.hex()}))
    catalog = admin.execute('select max(version) from control.schema_migrations').fetchone()[0]
    commit = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    (out / 'template.json').write_text(json.dumps({'schema': 'nexloop.load-template.v1', 'seed': seed, 'manifest_digest': synthetic.digest(lines),
        'manifest_totals': trailer['totals'], 'loaded': result['counts'], 'seconds': {**result['seconds'], 'total_load': load_seconds, 'pg_basebackup': backup_seconds},
        'tenant': TENANT, 'world': 'real', 'workbench_members': {'owner': 1, 'operator': 2}, 'commit': commit, 'catalog_revision': catalog,
        'not_loaded_in_slice_0': ['commercial_events', 'commitments', 'active_plans']}, ensure_ascii=False, indent=2) + '\n')
