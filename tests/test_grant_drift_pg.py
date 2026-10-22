"""E1 grant drift (docs/design/eios-capability-gap.md, L6 review I-2): the expected privileges come from a clean cluster bootstrapped from
this checkout's catalog (scripts/deploy/grant_drift.py expected); a second, independently bootstrapped database matches them exactly, and
one revoked grant, one added grant, one granted column and one added default privilege are each reported; an IGNORED pattern lists its
entry with its reason instead of failing. Read only on the checked database: the tool's catalog query runs in a read-only transaction."""
import json
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from nexloop_eios import grant_drift as G
from nexloop_eios.bootstrap import bootstrap, catalog

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import grant_drift as tool  # noqa: E402


@pytest.fixture(scope='module')
def expected_file(tmp_path_factory):
    out = tmp_path_factory.mktemp('grants') / 'expected.json'
    assert tool.main(['expected', '--out', str(out)]) == 0
    return out


# The test cluster's own harness (tests/support/loopback_effects.py: an event trigger schema) is not part of any release.
WHY = 'test harness only (tests/support/loopback_effects.py)'
HARNESS = tuple((p, WHY) for p in ('function|nexloop_test_harness.*|PUBLIC|*|*', 'function|nexloop_test_harness.*|<superuser>|*|*',
                                   'schema|nexloop_test_harness|<superuser>|*|*', 'owner|function:nexloop_test_harness.*|<superuser>|*|*',
                                   'owner|schema:nexloop_test_harness|<superuser>|*|*'))


@pytest.fixture
def live(admin, pg, tmp_path, monkeypatch):
    monkeypatch.setattr(G, 'IGNORED', HARNESS)
    bootstrap(admin)
    url = tmp_path / 'api-dsn'  # a role that can read the migration ledger (the revision check)
    url.write_text(make_conninfo(pg, user='nexloop_api'))
    url.chmod(0o600)
    return url


def run(capsys, *argv):
    code = tool.main(list(argv))
    return code, capsys.readouterr().out


def test_a_clean_database_matches_the_migrations_exactly(expected_file, live, capsys):
    snapshot = G.load(expected_file)
    assert snapshot['catalog_revision'] == catalog()[-1].version and len(snapshot['entries']) > 3000
    kinds = {e.split('|')[0] for e in snapshot['entries']}
    assert kinds == set(G.KINDS) - {'column'}  # the migrations grant no column
    code, out = run(capsys, 'check', '--database-url-file', str(live), '--expected', str(expected_file), '--json')
    result = json.loads(out)
    harness = len(result['ignored'])
    assert code == 0 and result['passed'] and harness and all(i['reason'] == WHY for i in result['ignored']), result
    assert result['counts'] == {'expected': len(snapshot['entries']), 'live': len(snapshot['entries']) + harness}
    assert result['live_revision'] == catalog()[-1].version and result['stale_ignores'] == []


def test_a_revoked_and_an_added_grant_are_both_reported(expected_file, live, admin, capsys):
    granted = 'relation|control.schema_migrations|nexloop_action_worker|SELECT|plain'
    assert granted in G.load(expected_file)['entries']
    table = granted.split('|')[1]
    admin.execute(f'revoke select on {table} from nexloop_action_worker')
    admin.execute('grant execute on function runtime.nexloop_world_kind(text) to nexloop_twin_port')
    admin.execute('grant update (status) on runtime.nexloop_local_artifacts to nexloop_metrics')
    admin.execute('alter default privileges for role nexloop_owner in schema runtime grant select on tables to nexloop_metrics')
    code, out = run(capsys, 'check', '--database-url-file', str(live), '--expected', str(expected_file), '--json')
    result = json.loads(out)
    assert code == 1 and result['passed'] is False
    assert result['missing'] == [granted]
    assert set(result['extra']) == {'function|runtime.nexloop_world_kind(text)|nexloop_twin_port|EXECUTE|plain',
                                    'column|runtime.nexloop_local_artifacts.status|nexloop_metrics|UPDATE|plain',
                                    'default|nexloop_owner/runtime/r|nexloop_metrics|SELECT|plain'}, result['extra']
    # The checked database was only read: the drift is still there, nothing was repaired.
    assert not admin.execute("select has_table_privilege('nexloop_action_worker',%s,'select')", (table,)).fetchone()[0]


def test_an_ignored_difference_is_listed_with_its_reason_and_not_counted(expected_file, live, admin):
    admin.execute('grant execute on function runtime.nexloop_world_kind(text) to nexloop_twin_port')
    with psycopg.connect(live.read_text(), autocommit=True) as c:
        actual = G.snapshot(c)
    ignore = (('function|runtime.nexloop_world_kind(text)|nexloop_twin_port|*|*', 'synthetic: reviewed difference'),)
    result = G.compare(G.load(expected_file)['entries'], actual, HARNESS + ignore)
    assert result['stale_ignores'] == []
    assert result['match'] and [i for i in result['ignored'] if 'harness' not in i['entry']] == [{'side': 'extra', 'entry': 'function|runtime.nexloop_world_kind(text)|nexloop_twin_port|EXECUTE|plain',
                                                      'reason': 'synthetic: reviewed difference'}]


def test_a_snapshot_of_another_revision_is_refused(expected_file, live, tmp_path, capsys):
    other = json.loads(expected_file.read_text())
    other['catalog_revision'] = '0001'
    stale = tmp_path / 'stale.json'
    stale.write_text(json.dumps(other))
    code, out = run(capsys, 'check', '--database-url-file', str(live), '--expected', str(stale))
    assert code == 1 and 'REVISION live 0' in out and 'snapshot 0001' in out, out


def test_a_role_that_cannot_read_the_ledger_needs_the_explicit_option(expected_file, live, pg, tmp_path, capsys):
    metrics = tmp_path / 'metrics-dsn'
    metrics.write_text(make_conninfo(pg, user='nexloop_metrics'))
    metrics.chmod(0o600)
    code, out = run(capsys, 'check', '--database-url-file', str(metrics), '--expected', str(expected_file))
    assert code == 1 and 'REVISION live None' in out and '0 missing, 0 extra' in out, out
    code, out = run(capsys, 'check', '--database-url-file', str(metrics), '--expected', str(expected_file), '--allow-unknown-revision')
    assert code == 0 and 'revision unknown' in out, out


def test_roles_memberships_the_database_and_owners_are_compared(expected_file, live, admin, capsys):
    admin.execute('alter role nexloop_metrics bypassrls')
    admin.execute('grant nexloop_owner to nexloop_api')
    admin.execute('grant pg_read_all_data to nexloop_metrics')
    admin.execute('alter role nexloop_api set search_path = public')
    admin.execute('grant temporary on database postgres to public')
    admin.execute('alter table control.schema_migrations owner to nexloop_api')
    admin.execute('create role nexloop_deploy_extra nologin')
    code, out = run(capsys, 'check', '--database-url-file', str(live), '--expected', str(expected_file), '--json')
    result = json.loads(out)
    assert code == 1
    assert {'role|nexloop_metrics|nexloop_metrics|BYPASSRLS|plain'} <= set(result['missing'])  # false expected ...
    assert {'role|nexloop_metrics|nexloop_metrics|BYPASSRLS|grantable',                    # ... true found
            'role|nexloop_api|nexloop_api|SET search_path=public|plain', 'database|<current>|PUBLIC|TEMPORARY|plain',
            'owner|relation:control.schema_migrations|nexloop_api|OWNER|plain', 'role|nexloop_deploy_extra|nexloop_deploy_extra|SUPERUSER|plain'} \
        <= set(result['extra']), result['extra']
    for role, member in (('nexloop_owner', 'nexloop_api'), ('pg_read_all_data', 'nexloop_metrics')):  # options follow the member's INHERIT
        assert any(e.startswith(f'member|{role}|{member}|MEMBER') for e in result['extra']), result['extra']
    # The api role's own search_path (set above) changes no name: every function entry still matches.
    assert not any(e.startswith(('function|', 'owner|function:')) for e in result['missing'] + result['extra'])


def test_the_live_side_is_read_only(live, admin):
    with psycopg.connect(live.read_text(), autocommit=True) as c:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction), c.transaction():
            G.read_only(c)
            c.execute("select set_config('search_path','pg_catalog',true)")
            c.execute('create temporary table nexloop_probe(x int)')


def test_an_ignore_pattern_must_name_kind_and_subject_and_a_stale_one_fails(expected_file, live):
    for pattern in ('*|x|PUBLIC|*|*', 'relation|x|*|*|*', 'relation|x', 'relation||PUBLIC|*|*'):
        with pytest.raises(ValueError):
            G.check_ignore(((pattern, 'a reviewed reason'),))
    with pytest.raises(ValueError):
        G.check_ignore((('relation|x|PUBLIC|*|*', 'short'),))
    with psycopg.connect(live.read_text(), autocommit=True) as c:
        actual = G.snapshot(c)
    stale = ('relation|runtime.no_such_table|PUBLIC|*|*', 'a pattern nothing matches any more')
    result = G.compare(G.load(expected_file)['entries'], actual, HARNESS + (stale,))
    assert not result['match'] and result['stale_ignores'] == [stale[0]]
    assert G.IGNORED == () or G.check_ignore(G.IGNORED)
