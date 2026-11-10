"""R76 step 8 (dispatcher 2026-11-10): `stage_secrets.py register --dry-run --bootstrap-dsn-file` on a host that holds only the
bootstrap DSN. The follow preview reads the Action lineage through the configurator role, so the dry run opens the configurator window
too: after the owner's Consumer v2 publication it prints follow_latest.derived (the three :2 grants of the run sheet), writes nothing
(authority facts and credential bindings unchanged) and the window is closed again (configurator NOLOGIN); a refused successor exits 2
with follow_latest_refused. Synthetic data."""
import json

from test_stage_secrets import S  # noqa: F401  (puts scripts/deploy on sys.path)
from test_assembly_dryrun_pg import TENANT, assembled, owner_workbench, stage, tree  # noqa: F401
from test_consumer_v2_stage_path_pg import BIND2, owner_publishes_consumer_v2

DIFF = [{'type': 'Consumer', 'from': 1, 'to': 2}]


def dry_run(st, capsys):
    capsys.readouterr()
    code = S.main(['register', '--out', str(st.out), '--tenant', TENANT, '--bootstrap-dsn-file', str(st.bootstrap_dsn), '--dry-run'])
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def written(admin):
    return {t: admin.execute(f"select md5(coalesce(string_agg(x::text, '|' order by x::text), '')) from {t} x").fetchone()[0]
            for t in ('authz.nexloop_authority_facts', 'authz.nexloop_service_credentials')}


def window_closed(admin):
    return admin.execute("select not rolcanlogin and rolpassword is null from pg_authid where rolname='nexloop_configurator'").fetchone()[0]


def test_dry_run_through_the_bootstrap_window_previews_the_follow_and_writes_nothing(assembled, admin, capsys):  # noqa: F811
    st = assembled
    owner_workbench(st, admin)
    owner_publishes_consumer_v2(st, admin)
    before = written(admin)
    code, out = dry_run(st, capsys)
    assert code == 0 and out['dry_run'] is True, out
    derived = sorted((d['principal'], d['resource_id'], d['scope_diff']) for d in out['follow_latest']['derived'])
    assert ('business_owner', 'eios:action:Consumer.create:2', DIFF) in derived
    assert [d for d in derived if d[1] == BIND2] == [('relay_planner', BIND2, DIFF), ('role_planner', BIND2, DIFF)]
    assert out['follow_latest']['refused'] == []
    assert written(admin) == before and window_closed(admin)


def test_a_refused_successor_stops_the_dry_run_with_follow_latest_refused(assembled, admin, capsys, monkeypatch):  # noqa: F811
    from nexloop_eios import service_grants as G
    st = assembled
    owner_workbench(st, admin)
    owner_publishes_consumer_v2(st, admin)
    real = G.follow_preview

    def refusing(manifest, lineage):
        preview = real(manifest, lineage)
        assert preview['derived']  # the lineage was read through the window
        return {**preview, 'refused': [{'principal': 'business_owner', 'resource_id': 'eios:action:Consumer.create:1', 'reason': 'create_scope_other_type'}]}
    monkeypatch.setattr(G, 'follow_preview', refusing)
    before = written(admin)
    code, out = dry_run(st, capsys)
    assert (code, out) == (2, {'ok': False, 'error': 'ServiceGrantsRejected', 'detail': 'follow_latest_refused'})
    assert written(admin) == before and window_closed(admin)
